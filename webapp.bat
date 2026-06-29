@echo off
chcp 65001 >nul
REM ============================================================================
REM  WEBAPP - the FastAPI + PWA control dashboard (the product).
REM  HTTPS on :8447 when webapp\certificates\cert.pem is present, else HTTP.
REM  Binds 0.0.0.0 so the LAN + Tailscale hostnames reach it.
REM  Daily use will be via the tray (issue #2); this bat is dev / headless.
REM ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [ERROR] .venv missing. Create it and run: .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

cd /d "%SCRIPT_DIR%" || exit /b 1

set "CERT=%SCRIPT_DIR%webapp\certificates\cert.pem"
set "KEY=%SCRIPT_DIR%webapp\certificates\key.pem"

REM Auto-renew the Tailscale cert if it is expiring within 30 days (no-op
REM when the cert is missing or not a .ts.net cert).
"%VENV_PY%" "%SCRIPT_DIR%scripts\gen_tailscale_cert.py" --check

if not exist "%CERT%" (
    echo [INFO] No HTTPS cert found, running HTTP-only on :8447.
    echo        Run scripts\gen_tailscale_cert.py to enable HTTPS.
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447 --no-access-log
) else (
    echo [INFO] HTTPS via %CERT%
    "%VENV_PY%" -m uvicorn app.webapp.server:app --host 0.0.0.0 --port 8447 --ssl-keyfile "%KEY%" --ssl-certfile "%CERT%" --no-access-log
)

exit /b %ERRORLEVEL%
