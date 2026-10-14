# Start backend (FastAPI :8000) and frontend (Streamlit :8501) in two windows.
# Usage:  powershell -ExecutionPolicy Bypass -File run.ps1 [-Seed]
param([switch]$Seed)
$root = $PSScriptRoot
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }

if (-not (Test-Path "$root\backend\.env")) { Copy-Item "$root\backend\.env.example" "$root\backend\.env"; Write-Host "Created backend\.env from template" }

Push-Location "$root\backend"
& $py -m alembic upgrade head
if ($Seed) { & $py -m app.bootstrap --seed }
Pop-Location

Start-Process -WorkingDirectory "$root\backend" -FilePath $py -ArgumentList "-m uvicorn app.main:app --port 8000"
Start-Process -WorkingDirectory "$root\frontend" -FilePath $py -ArgumentList "-m streamlit run app.py --server.port 8501"
Write-Host "API      -> http://127.0.0.1:8000/docs"
Write-Host "Web app  -> http://localhost:8501"
