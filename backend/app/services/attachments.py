"""Customer image uploads, treated as hostile input.

Pipeline for every file:
    size limit -> decode with Pillow (must be a real JPEG/PNG/WEBP, not just the right extension)
    -> decompression-bomb guard -> downscale if huge -> RE-ENCODE (drops EXIF/GPS metadata and any
    payload appended to the file) -> store under a random name outside the web root.

Validation happens BEFORE the ticket is created, so a bad upload never costs an LLM call.
"""
import io
import logging
import re
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, ImageOps

from ..core.config import get_settings
from ..db.models import Attachment, Ticket

log = logging.getLogger(__name__)

FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
Image.MAX_IMAGE_PIXELS = 40_000_000          # ~40 MP: refuse decompression bombs


@dataclass
class PreparedImage:
    data: bytes
    original_name: str
    content_type: str
    ext: str
    width: int
    height: int


def _safe_name(name: str | None) -> str:
    base = Path(name or "image").name
    return (re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:100]) or "image"


def prepare_images(files: list[UploadFile]) -> list[PreparedImage]:
    s = get_settings()
    files = [f for f in files if f and f.filename]
    if len(files) > s.max_images_per_ticket:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"At most {s.max_images_per_ticket} images per ticket")
    max_bytes = int(s.max_upload_mb * 1024 * 1024)
    out: list[PreparedImage] = []
    for f in files:
        raw = f.file.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                f"{_safe_name(f.filename)} is larger than {s.max_upload_mb:g} MB")
        out.append(_process(raw, _safe_name(f.filename), s.max_image_pixels))
    return out


def _process(raw: bytes, name: str, max_side: int) -> PreparedImage:
    invalid = HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{name} is not a valid JPEG, PNG or WEBP image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                fmt = probe.format
                probe.verify()                       # structural check
            img = Image.open(io.BytesIO(raw))        # verify() invalidates the handle; reopen
            img.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} has too many pixels")
    except Exception:
        raise invalid
    if fmt not in FORMATS:
        raise invalid

    img = ImageOps.exif_transpose(img)               # keep the photo upright before dropping EXIF
    img.thumbnail((max_side, max_side))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    content_type, ext = FORMATS[fmt]
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    else:
        img.save(buf, fmt)                            # fresh encode: no metadata, no trailing bytes
    return PreparedImage(buf.getvalue(), name, content_type, ext, img.width, img.height)


def upload_root() -> Path:
    root = Path(get_settings().upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_images(ticket: Ticket, images: list[PreparedImage]) -> list[Path]:
    """Write files and attach DB rows. Returns written paths so the caller can clean up on failure."""
    root, written = upload_root(), []
    for im in images:
        stored = f"{uuid.uuid4().hex}.{im.ext}"
        path = root / stored
        path.write_bytes(im.data)
        written.append(path)
        ticket.attachments.append(Attachment(stored_name=stored, original_name=im.original_name,
                                             content_type=im.content_type, size_bytes=len(im.data),
                                             width=im.width, height=im.height))
    return written


def attachment_path(att: Attachment) -> Path:
    root = upload_root().resolve()
    path = (root / att.stored_name).resolve()
    if root not in path.parents:                       # defence in depth against path tricks
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    if not path.exists():
        log.error("attachment file missing", extra={"attachment_id": att.id})
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return path
