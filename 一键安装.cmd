@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title HeronBo 视频工作台 · 一键安装

echo ============================================================
echo   HeronBo · AI 视频工作台 —— 一键安装
echo   （找 Python → 装依赖 → 接 agent 通道 → 建桌面快捷方式 → 起工作台）
echo ============================================================
echo.

rem ── 1. 找 Python ─────────────────────────────────────────────
set "PY="
for %%C in ("py -3.12" "py -3.13" "py -3" "python") do (
  if not defined PY (
    %%~C -c "import sys" >nul 2>nul && set "PY=%%~C"
  )
)
if not defined PY (
  for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
  ) do (
    if not defined PY if exist %%P set "PY=%%~P"
  )
)
if not defined PY (
  echo [停] 这台电脑没找到 Python。
  echo      工作台需要 Python 3.9+（勾上 "Add python.exe to PATH" 再安装）。
  echo      下载：https://www.python.org/downloads/windows/
  echo      装完把本窗口关掉，重新双击一次「一键安装.cmd」。
  echo.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
echo [1/5] Python：%PY%
echo.

echo [2/5] 依赖（优先用仓库里带的离线包，装得快）
%PY% "tools\部署.py" install --yes
echo.
echo [3/5] agent 通道（哪台电脑装了什么就用什么；跑一次最小连通测试）
%PY% "tools\部署.py" agents --yes
echo.
echo [4/5] 桌面快捷方式
%PY% "tools\部署.py" shortcut --yes
echo.
echo [5/5] 起工作台
%PY% "tools\部署.py" start --yes
echo.
echo ------------------------------------------------------------
echo  装完了。桌面上会有「HeronBo 视频工作台」，双击即可打开。
echo  想改样本库根/看体检：%PY% "tools\部署.py" check
echo  独立窗口（pywebview）起不来的话，工作台会自动改用 Edge 窗口打开。
echo ------------------------------------------------------------
pause
