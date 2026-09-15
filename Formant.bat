@echo off
chcp 65001 >nul
title Formant 1.6
cd /d "%~dp0"
echo.
echo ============================================
echo   Formant 1.6
echo ============================================
echo.

if not exist "formant.py" (
  echo [ERROR] formant.py not found. Unzip the whole folder.
  pause
  exit /b 1
)
if not exist "ui\index.html" (
  echo [ERROR] ui folder missing. Unzip the whole Formant folder.
  pause
  exit /b 1
)

echo Installing Python packages if needed...
where py >nul 2>&1 && (
  py -3 -m pip install -r "%~dp0requirements.txt" -q
  echo Starting Formant...
  py -3 formant.py
) || (
  python -m pip install -r "%~dp0requirements.txt" -q
  echo Starting Formant...
  python formant.py
)
if errorlevel 1 (
  echo.
  echo Formant could not start. Python 3 is required.
  pause
)
