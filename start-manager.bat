@echo off
rem Starts the portfolio manager: double-click this file, keep the window open while you work.
cd /d "%~dp0"
title Portfolio manager

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from python.org ^(tick "Add Python to PATH"^), then run this again.
  pause
  exit /b 1
)

rem Pillow is needed to convert images to AVIF; install it once if it is missing.
%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo Installing Pillow ^(one time only^)...
  %PY% -m pip install --upgrade pillow
)

%PY% tools\manager-server.py
pause
