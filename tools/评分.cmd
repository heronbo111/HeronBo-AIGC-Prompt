@echo off
chcp 65001 >nul
python "%~dp0\评分.py" %*
if errorlevel 1 pause
