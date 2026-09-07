chcp 65001
@echo off
rem ============================================================
rem  Meeting Subtitle Tool - Launcher
rem  Double-click to start. Auto cd to script dir.
rem  Note: ASCII-only content to avoid codepage issues.
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] python not found: .venv\Scripts\python.exe
    echo Please create the virtual environment first by running: uv venv
    echo or install dependencies with: uv sync
    pause
    exit /b 1
)

echo Starting Meeting Subtitle Tool ...
echo (Subtitle window and Control window will appear.
echo  Closing the Control window exits the program.)
echo.

".venv\Scripts\python.exe" gui_launcher.py

if errorlevel 1 (
    echo.
    echo [INFO] Program exited abnormally.
    echo Check logs\app.log for details.
    pause
)