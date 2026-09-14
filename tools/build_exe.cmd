@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem ── 选一个「带 tkinter」的解释器 ─────────────────────────────────────────
rem 没 tkinter 的 python 打出来的 exe 会一启动就崩，所以必须先挑对解释器。
set "PYEXE="
for %%C in ("py -3.12" "py -3.13" "py -3" "python") do (
  if not defined PYEXE (
    %%~C -c "import tkinter" >nul 2>nul && set "PYEXE=%%~C"
  )
)
if not defined PYEXE (
  echo.
  echo [失败] 没找到带 tkinter 的 Python。
  echo        装一个含 tcl/tk 的 Python 3（安装时勾上 tcl/tk and IDLE），再跑本脚本。
  echo.
  pause
  exit /b 1
)
echo 使用解释器: %PYEXE%

%PYEXE% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [失败] 这个解释器还没装 PyInstaller，先跑：
  echo        %PYEXE% -m pip install pyinstaller
  echo.
  pause
  exit /b 1
)

%PYEXE% -m PyInstaller --noconfirm --onefile --windowed --name score-tool ^
  --hidden-import json --hidden-import datetime --hidden-import argparse ^
  --add-data "score_core.py;." ^
  score_gui.pyw
if errorlevel 1 (
  echo.
  echo [失败] 打包没成功，看上面的报错。
) else (
  echo.
  echo build done: dist\score-tool.exe
)
echo.
pause
