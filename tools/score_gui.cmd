@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0dist\score-tool.exe" (
  start "" "%~dp0dist\score-tool.exe" %*
  exit /b 0
)
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"
start "" %PYW% "%~dp0score_gui.pyw" %*
