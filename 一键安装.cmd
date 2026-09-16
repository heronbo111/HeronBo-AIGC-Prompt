@echo off
rem HeronBo workbench one-click installer. Deliberately ASCII-only: cmd.exe parses
rem .cmd byte-by-byte in the active code page, so UTF-8 Chinese here gets chopped up
rem (we hit "'x' is not recognized" that way). All Chinese text is printed by Python,
rem and the deploy script is called through its ASCII alias tools\deploy.py.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title HeronBo Workbench - install
echo ============================================================
echo   HeronBo - AI Video Workbench   [ one-click install ]
echo   dependencies - agent channel - desktop shortcut - launch
echo ============================================================
echo.
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  goto :run
)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
  goto :run
)
echo [STOP] Python 3.9+ not found.
echo        Install Python and tick "Add python.exe to PATH", then run this file again.
echo        Your browser is opening the download page now.
start "" "https://www.python.org/downloads/windows/"
echo.
pause
exit /b 1
:run
echo [1/2] deploy script   (via %PY%)
echo.
%PY% "tools\deploy.py" all --yes
if errorlevel 1 goto :fail
echo.
echo [2/2] finished. There should be a HeronBo shortcut on your desktop now.
echo       health check  :  %PY% "tools\deploy.py" check
echo       blank window? :  see docs/ in the repo (new machine guide)
echo.
pause
exit /b 0
:fail
echo.
echo [STOP] Something failed above - send those lines to your agent.
echo        then re-run the health check to see what is still missing.
echo.
pause
exit /b 1
