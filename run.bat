@echo off
REM ---------------------------------------------------------------------
REM  AI Shorts Maker launcher
REM  NOTE: This file is intentionally ASCII-only. cmd.exe reads .bat files
REM        using the console code page, so non-ASCII text here breaks
REM        parsing on some systems. Korean docs are in README.md.
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   AI Shorts Maker
echo ============================================
echo.

REM ---------- Python ----------
set "PYCMD="
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
    if !errorlevel!==0 set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>&1
    if !errorlevel!==0 (
        python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
        if !errorlevel!==0 set "PYCMD=python"
    )
)
if not defined PYCMD (
    echo [ERROR] Python 3.11 or newer not found.
    echo.
    echo   How to install:
    echo     1^) https://www.python.org/downloads/windows/
    echo        Check "Add python.exe to PATH" during setup.
    echo     2^) or in PowerShell:  winget install --id Python.Python.3.12 -e
    echo.
    echo   Then close this window and run run.bat again.
    echo   See README.md for details in Korean.
    pause
    exit /b 1
)
echo [1/4] Python OK  ^(%PYCMD%^)

REM ---------- FFmpeg ----------
set "FFOK="
where ffmpeg >nul 2>&1
if %errorlevel%==0 set "FFOK=1"
if not defined FFOK if exist "%~dp0bin\ffmpeg.exe" (
    set "PATH=%~dp0bin;%PATH%"
    set "FFOK=1"
)
if not defined FFOK if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
    set "FFOK=1"
)
if not defined FFOK if exist "C:\ffmpeg\bin\ffmpeg.exe" (
    set "PATH=C:\ffmpeg\bin;%PATH%"
    set "FFOK=1"
)
if defined FFOK (
    echo [2/4] FFmpeg OK
) else (
    echo [WARNING] FFmpeg not found. Rendering will not work.
    echo.
    echo   How to install:
    echo     1^) PowerShell:  winget install --id Gyan.FFmpeg -e
    echo     2^) or download a "full build" from
    echo        https://www.gyan.dev/ffmpeg/builds/
    echo        unzip to C:\ffmpeg and add C:\ffmpeg\bin to PATH
    echo     3^) or copy ffmpeg.exe / ffprobe.exe into this folder's bin\
    echo.
    echo   After installing, open a NEW window and run run.bat again.
    echo   See README.md for details in Korean.
    echo.
    pause
)

REM ---------- venv ----------
if not exist ".venv\Scripts\python.exe" (
    echo [3/4] Creating virtual environment...
    %PYCMD% -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    echo       Installing packages... ^(first run only, may take a few minutes^)
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if not !errorlevel!==0 (
        echo [ERROR] Failed to install packages.
        pause
        exit /b 1
    )
) else (
    echo [3/4] Virtual environment OK
    ".venv\Scripts\python.exe" -c "import streamlit" >nul 2>&1
    if not !errorlevel!==0 (
        echo       Reinstalling packages...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    )
)

REM ---------- run ----------
echo [4/4] Starting Streamlit at http://localhost:8501
echo       Your browser opens in a few seconds. Press Ctrl+C here to stop.
echo.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
start "" /min cmd /c "timeout /t 6 /nobreak >nul & start "" http://localhost:8501"
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 --server.headless true --server.maxUploadSize 2048

pause
