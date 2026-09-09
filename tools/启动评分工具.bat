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
rem 可选：把样本库根目录作为第一个参数传入完成首次登记；不传则校验 references/paths.md 的取值
set "ROOT_ARG=%~1"
if defined ROOT_ARG (
  %PY% "%~dp0首次配置.py" --set "%ROOT_ARG%"
) else (
  %PY% "%~dp0首次配置.py"
)
if errorlevel 1 (
  echo.
  echo 还没指定样本库根目录。先对 agent 说：请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类
  echo 或重新运行：启动评分工具.bat "你的样本库根目录"
  echo.
)
start "评价工坊服务" /min %PY% -m http.server 8787 --directory "%~dp0"
timeout /t 1 >nul
start "" "http://localhost:8787/评价工具.html"
echo 已打开页面。首次点「连接样本目录」选中样本库根（之后自动记忆）；用完关闭任务栏「评价工坊服务」窗口即停服。
