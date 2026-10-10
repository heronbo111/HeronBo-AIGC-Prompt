@echo off
rem One-click SKILL installer (no workbench, no Python deps, no network).
rem ASCII-only on purpose: cmd.exe parses .cmd byte-by-byte in the active code
rem page, so UTF-8 Chinese here gets chopped up. All text is printed by Python.
rem What it does: run install_skill.py --auto  ->  installs the skill folder
rem into the detected agent skills dir (~/.zcode/skills/ by default on ZCode
rem machines) and junction-links other harnesses found on this machine.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title HeronBo-AIGC-Prompt skill - install
echo ============================================================
echo   HeronBo-AIGC-Prompt  [ skill one-click install ]
echo   prompt brain + tools only - NO workbench, NO pip installs
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
echo        Install Python (tick "Add python.exe to PATH") and run again.
echo        Opening the download page now.
start "" "https://www.python.org/downloads/windows/"
echo.
pause
exit /b 1
:run
echo [1/2] running installer via %PY%
echo.
%PY% "install_skill.py" --auto
if errorlevel 1 goto :fail
echo.
echo [2/2] done. Open a NEW conversation with your agent and say:
echo       "帮我写提示词"   (or: 读取 <安装目录>\SKILL.md 并按其工作流执行)
echo       On first use the agent will ask where to put your video projects.
echo.
echo       Optional GUI workbench: download the full Setup from
echo       https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/latest
echo.
pause
exit /b 0
:fail
echo.
echo [STOP] Something failed above - send those lines to your agent.
echo.
pause
exit /b 1
