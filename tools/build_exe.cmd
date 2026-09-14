@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py -3"
%PYEXE% -m PyInstaller --noconfirm --onefile --windowed --name score-tool ^
  --hidden-import json --hidden-import datetime --hidden-import argparse ^
  --add-data "score_core.py;." ^
  score_gui.pyw
if errorlevel 1 (
  echo.
  echo [失败] 打包没成功。若提示缺少 PyInstaller，先跑：%PYEXE% -m pip install pyinstaller
) else (
  echo.
  echo build done: dist\score-tool.exe
)
echo.
pause
