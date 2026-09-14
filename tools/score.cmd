@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py -3"
%PYEXE% score.py %*
echo.
pause
