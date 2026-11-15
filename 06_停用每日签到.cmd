@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
schtasks /Change /TN "学习辅导-每日签到" /Disable
pause
