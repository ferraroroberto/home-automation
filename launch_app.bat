@echo off
REM ==========================================================
REM  Launch the Streamlit POC SPIKE locally (browser opens automatically).
REM  This is the throwaway data/debug view, NOT the product — the real
REM  control surface is the FastAPI + PWA webapp (see webapp.bat).
REM ==========================================================
title Home Automation - MELCloud (spike)
cd /d "%~dp0"

echo ============================================================
echo   Home Automation - MELCloud Streamlit SPIKE (local)
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

".venv\Scripts\python.exe" -m streamlit run "spike\streamlit_app.py" --browser.gatherUsageStats=false
pause
