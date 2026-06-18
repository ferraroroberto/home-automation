@echo off
chcp 65001 >nul
REM ============================================================================
REM  HomeAutomation TRAY - tray icon that owns a long-lived service lifecycle
REM ----------------------------------------------------------------------------
REM  CANONICAL TEMPLATE. Copy to `tray.bat` in a tray-resident app, then replace
REM  the four __PLACEHOLDER__ tokens (marked `=== ADAPT ===`). Everything else is
REM  the orphan-proof reclaim-then-start machinery and is copied verbatim, so a
REM  filled-in copy is byte-identical to every sister tray. Full reasoning:
REM  scaffold docs/windows-tray.md + project-scaffolding#29.
REM
REM  Launch this on login (Startup folder) for an always-on service.
REM
REM  Idempotent:
REM    tray.bat              -> no-op if a HomeAutomation tray is already running
REM    tray.bat --restart    -> stop the running tray (and its service tree) and
REM                             start a fresh one
REM
REM  Detection matches the tray process by command line + this project's .venv
REM  path via CIM, then kills BY PID with /T. We never blanket-kill pythonw, so
REM  sister-app trays and any other python processes are untouched.
REM
REM  The CIM detection + port reclaim live in app\tray\tray_lifecycle.ps1 (a
REM  committed helper shelled to with -File), NOT inline `powershell -Command
REM  "..."`. Inline, the CIM -Filter's nested quotes inside this batch's quotes
REM  inside a `for /f usebackq` backtick block get mangled when tray.bat is
REM  launched non-interactively (Git Bash -> `cmd /c "tray.bat --restart"`, or a
REM  finisher skill's Bash tool): the inline command returns empty, nothing is
REM  killed, and --restart silently degrades to a plain start that adopts the
REM  stale webapp and reports success. -File removes the nested quoting, so
REM  detection/reclaim behave identically from any caller (project-scaffolding#54).
REM
REM  --restart is orphan-proof: besides killing the tray subtree, it reclaims
REM  this app's owned service ports by their owning PID, regardless of process
REM  parentage. A service child that got detached from its tray (a stale process
REM  from an earlier run) would otherwise survive a subtree kill, block the fresh
REM  tray from binding, and keep serving the old build while the restart reports
REM  success. The reclaim is scoped to processes whose CommandLine is under THIS
REM  repo's .venv (NOT the process image path): a venv-launched pythonw re-execs
REM  the base interpreter, so .Path reports the shared base python while only the
REM  CommandLine still carries the .venv path. Matching the image path would miss
REM  the real service; the CommandLine scope keeps the sweep on THIS repo only.
REM
REM  Mutex-shared ports (a port another app may legitimately own) must NOT go in
REM  the 8447 reclaim list -- reclaiming one would kill the sibling.
REM ============================================================================

setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv\Scripts"
set "VENV_PYW=%VENV_DIR%\pythonw.exe"
set "VENV_PY=%VENV_DIR%\python.exe"

cd /d "%SCRIPT_DIR%" || exit /b 1

REM === ADAPT (1/4): short app name, used in messages + the start window title ===
set "APP_NAME=HomeAutomation"
REM === ADAPT (2/4): the args python is started with to launch the tray,
REM     e.g. "launcher.py tray"  or  "-m tray" ===
set "TRAY_LAUNCH=-m app.tray"

set "WANT_RESTART="
if /i "%~1"=="--restart" set "WANT_RESTART=1"
if /i "%~1"=="-r"        set "WANT_RESTART=1"

REM === ADAPT (3/4): in the -TrayMatch below, replace __TRAY_MATCH__ with a regex
REM     matching THIS app's tray invocation, e.g. launcher\.py\s+tray  or  -m\s+tray
set "PS=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
set "TRAY_VENV=%SCRIPT_DIR%.venv"
set "TRAY_PS=%SCRIPT_DIR%app\tray\tray_lifecycle.ps1"
if not exist "%TRAY_PS%" (
    echo ERROR: missing tray helper "%TRAY_PS%" -- vendor app\tray\tray_lifecycle.ps1 from the scaffold.
    exit /b 1
)
set "TRAY_PIDS="
for /f "usebackq delims=" %%P in (`%PS% -NoProfile -NonInteractive -File "%TRAY_PS%" detect -VenvDir "%TRAY_VENV%" -TrayMatch "app\.tray"`) do (
    if defined TRAY_PIDS (set "TRAY_PIDS=!TRAY_PIDS! %%P") else (set "TRAY_PIDS=%%P")
)

if defined TRAY_PIDS if not defined WANT_RESTART (
    echo %APP_NAME% tray is already running ^(PID: !TRAY_PIDS!^).
    echo Run "tray.bat --restart" to stop it and start fresh.
    exit /b 0
)

if defined WANT_RESTART (
    if defined TRAY_PIDS (
        echo Stopping previous %APP_NAME% tray ^(PID: !TRAY_PIDS!^)...
        for %%P in (!TRAY_PIDS!) do (
            taskkill /T /F /PID %%P >nul 2>&1
        )
    )
    REM Orphan-proof: reclaim this app's owned service ports from ANY holder
    REM whose CommandLine is under this repo's .venv, even one detached from the
    REM tray subtree above. CommandLine scope (not image path) is load-bearing --
    REM see the header note.
    REM === ADAPT (4/4): replace __OWNED_PORTS__ with this tray's exclusively-owned
    REM     ports as a comma list, e.g. 8445,8446 . Exclude any mutex-shared port. ===
    set "RECLAIM_VENV=%SCRIPT_DIR%.venv"
    %PS% -NoProfile -NonInteractive -File "%TRAY_PS%" reclaim -VenvDir "!RECLAIM_VENV!" -Ports "8447"
    REM Give Windows a moment to release the ports before rebinding.
    ping 127.0.0.1 -n 3 >nul
)

REM Prefer pythonw.exe so no console window stays open. The window title
REM differentiates this tray from sister apps' trays.
if exist "%VENV_PYW%" (
    start "%APP_NAME% Tray" "%VENV_PYW%" %TRAY_LAUNCH%
) else if exist "%VENV_PY%" (
    start "%APP_NAME% Tray" "%VENV_PY%" %TRAY_LAUNCH%
) else (
    start "%APP_NAME% Tray" pythonw %TRAY_LAUNCH%
)
exit /b 0
