@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo Once kurulum_windows.bat dosyasini calistirin.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
set PYTHONPATH=src
echo Durdurmak icin bu pencerede Ctrl+C tuslarina basin.
python -m cryptobot
pause
