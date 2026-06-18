@echo off
REM ==========================================================
REM  Launch the Streamlit app locally (browser opens automatically)
REM ==========================================================
title Home Automation - MELCloud
cd /d "%~dp0"

echo ============================================================
echo   Home Automation - MELCloud device check (local)
echo   URL will print below (default http://localhost:8501)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m streamlit run "app\app.py" --browser.gatherUsageStats=false
pause
