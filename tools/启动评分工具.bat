@echo off
chcp 65001 >nul
echo 启动 Seedance 评价工坊服务（http://localhost:8787）...
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
  echo 未找到 python，请先安装 Python 3 并加入 PATH，然后重试。
  pause
  exit /b 1
)
start "评价工坊服务" /min %PY% -m http.server 8787 --directory "%~dp0"
timeout /t 1 >nul
start "" "http://localhost:8787/评价工具.html"
echo 已打开浏览器页面。用完后关闭任务栏「评价工坊服务」小窗口即停止服务。
