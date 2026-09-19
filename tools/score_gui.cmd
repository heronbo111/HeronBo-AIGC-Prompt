@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ① 首选打包好的 exe：自带解释器，完全不依赖本机 Python 环境
if exist "%~dp0dist\score-tool.exe" (
  start "" "%~dp0dist\score-tool.exe" %*
  exit /b 0
)

rem ② 退回源码：挑一个「真的能 import tkinter」的解释器
rem    不能盲用 PATH 上的 pythonw —— 它可能是没带 tcl/tk 的绿色版，一启动就崩。
set "PYW="
for %%C in ("py -3.12" "py -3.13" "py -3.11" "py -3" "pythonw" "python") do (
  if not defined PYW (
    %%~C -c "import tkinter" >nul 2>nul && set "PYW=%%~C"
  )
)

if not defined PYW (
  echo.
  echo [失败] 既没有 dist\score-tool.exe，也没找到「带 tkinter」的 Python。
  echo.
  echo   解决办法二选一：
  echo     1) 跑 python tools\部署.py vendor --fetch --yes，把官方 exe 放进 dist\；
  echo     2) 安装 Python 3 时勾上 "tcl/tk and IDLE"。
  echo.
  pause
  exit /b 1
)

echo 使用解释器: %PYW%
start "" %PYW% "%~dp0score_gui.pyw" %*
