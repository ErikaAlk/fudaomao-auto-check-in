@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 请先运行 01_安装环境.cmd。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0install_task.py"
set "taskExit=%errorlevel%"
pause
exit /b %taskExit%
