@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
py -3.12 -V >nul 2>&1
if errorlevel 1 (
  python "%~dp0bootstrap.py"
) else (
  py -3.12 "%~dp0bootstrap.py"
)
set "setupExit=%errorlevel%"
if not "%setupExit%"=="0" echo 安装未完成，请按使用说明检查 Python 3.12 64 位环境。
pause
exit /b %setupExit%
