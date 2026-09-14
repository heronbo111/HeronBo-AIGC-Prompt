@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py -3"
%PYEXE% -m PyInstaller --noconfirm --onefile --windowed --name score-tool --hidden-import json --hidden-import datetime --hidden-import argparse --add-data "score_core.py;." --add-data "../references/paths.local.md;references" score_gui.pyw
echo.
echo build done: dist\score-tool.exe
pause
