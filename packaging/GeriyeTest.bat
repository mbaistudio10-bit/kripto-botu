@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Gecmis veri testi basliyor (birkac dakika surebilir)...
KriptoBotu.exe --backtest
