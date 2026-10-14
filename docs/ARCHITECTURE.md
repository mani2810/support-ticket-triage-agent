# FreshCart Support: Architecture & Design Decisions

This document records *why* the system looks the way it does, the trade-offs we accepted, and what to change when it outgrows a single machine.

## 1. Layering

```
frontend (Streamlit)  ──HTTP/JSON──►  api/        routes: validation, auth, role checks (thin)
                                       services/   use-cases: tickets.py (persist + audit), triage.py (LangGraph decision)
                                                   llm.py: the ONLY module that talks to Groq (gateway)
                                       db/         SQLAlchemy models + session
                                       core/       config, security, logging, rate limiting
```

- **The frontend is a pure API client.** It has no DB access and no secrets, so it could be replaced with React or a mobile app without touching the backend.
- **The routes are thin.** Business rules live in `services/`, so they are unit-testable without HTTP.
- **The triage graph decides; it does not write.** `services/tickets.py` persists the decision and the audit events in one transaction. This lets the same graph serve *new ticket*, *re-route* and *re-classify*.

## 2. LLM usage: decisions

| Decision | Rationale |
|---|---|
| One structured-output call per ticket (category, priority and reason together) | Cheapest and fastest option; it also keeps the three fields consistent with each other. |
| `with_structured_output(TicketClassification)` plus Pydantic | The model can only answer with allowed values; free text is sanitised and length-capped. |
| Business rules run **after** the LLM (`prioritize`) | Must-hold policies (e.g. a high-value refund is always P1) never depend on model behaviour. |
| Rule-based fallback classifier | The app is fully usable with no key, no network, or Groq down. |
| A gateway owns retries (`ChatGroq(max_retries=0)`) | No hidden fan-out of calls; one place for metrics and policy. |
| Errors are classified: transient vs configuration vs bad output | Retry only what can succeed. A wrong key or model opens the circuit for 10 minutes (it will not fix itself). Bad output falls back without blaming the provider. |
| Circuit breaker (closed → open → half-open with a single probe) | During an outage, requests don't wait on timeouts; recovery is detected automatically. |
| Sliding-window call budget | Proactively stay under free-tier RPM instead of reacting to 429s. |
| TTL cache keyed on model + prompt version + normalised text | Duplicate submissions or repeats cost 0 calls. Bump `PROMPT_VERSION` when the prompt changes. |
| *Re-route* reuses the stored classification | Reassigning after staff changes is the common admin action and needs no AI. |

## 3. Customer portal & AI assist: decisions

| Decision | Rationale |
|---|---|
| Tickets are raised **from an order** and identity comes from the session | No typos in order IDs or emails; prevents raising tickets on someone else's order (ownership is checked server-side). |
| One open ticket per order | Avoids duplicates and wasted LLM calls; the customer is pointed to the existing ticket. |
| Order context goes to the routing LLM | "Damaged honey jar, order ₹782, delivered on time" routes better than free text alone. |
| Images are validated **before** triage | A rejected upload never costs an LLM request or leaves a half-created ticket. Files are cleaned up if the DB commit fails. |
| AI assist is on demand, stored, and advisory | Cost control (0 calls when re-opened), auditability (who asked, what was suggested), human accountability. |
| Money rules live in code, not in the prompt | LLMs can be talked into things; `apply_guardrails()` cannot. Refunds are capped by real order data, items must exist, and large refunds need approval. |
| Customers see a filtered timeline | Internal notes, AI reasoning and staff emails stay internal (`CUSTOMER_VISIBLE` events only). |

## 4. Security model

- **Trust boundary:** everything from customers (and everything the LLM returns) is untrusted. It is validated by Pydantic, length-limited and markdown-escaped before rendering, and the prompt explicitly says not to follow instructions inside the ticket.
- **AuthN:** OAuth2 password flow issues a short-lived signed JWT (HS256, `iss`/`exp`/`iat` required). The secret is per-installation (auto-generated locally, mandatory in production).
- **AuthZ:** role and active status are re-read from the DB on each request, so revoking access is immediate. Resource-level checks (agent ↔ own tickets) return 404 to avoid existence leaks.
- **Brute force:** per-IP rate limit, per-account lockout, constant-time path for unknown users.
- **Secrets hygiene:** `.env` and `.secrets/` are git-ignored, logs redact keys, tokens and passwords, and startup warns about demo credentials.

## 5. Observability

- Request-id correlation across the frontend → API → LLM gateway → DB writes.
- An `audit` logger for security events, plus a per-ticket activity log in the DB for business events.
- Gateway metrics (calls, cache hits, fallbacks, retries, breaker state) on the admin dashboard; `/ready` exposes DB and circuit health.

## 6. Known limits & how to scale

| Limit (single-process by design) | When it matters | Upgrade path |
|---|---|---|
| Rate limiter, login lockout, LLM cache, breaker and budget are **in-memory** | More than one backend instance or worker | Move them to Redis (`slowapi` supports `storage_uri="redis://..."`) |
| The LLM call runs inside the HTTP request (sync, threadpool) | High submission volume | Queue tickets (e.g. Celery or RQ) and classify asynchronously; the UI shows "triaging…" |
| SQLite | Concurrent writers, multiple instances | PostgreSQL via `DATABASE_URL` (driver already included) |
| No refresh tokens or token revocation list | Long sessions, forced logout | Short access tokens + refresh tokens, or a `token_version` column checked per request |
| Users cannot change their own password (admin resets it) | Real teams | Add `/auth/change-password` and force a change on first login |
| Two agents can be chosen concurrently under heavy load | Bursty traffic | `SELECT … FOR UPDATE` on PostgreSQL, or assign from a queue worker |
| Images stored on local disk | Several instances / containers | Object storage (S3 / Azure Blob) behind the same `attachments` service |
| The AI assist reads text only (not the photos) | Visual damage assessment | Add a vision-capable model to `suggest()`; photos are already validated and stored |
| No HTTPS in the app itself | Any network exposure | Run behind a reverse proxy (Caddy or Nginx) or platform TLS |
