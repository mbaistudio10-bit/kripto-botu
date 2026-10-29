@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo GERCEK PARA MODU. Durdurmak icin bu pencerede Ctrl+C tuslarina basin.
echo Once botun arka planda calismadigindan emin olun (ArkaPlan-Durdur.bat).
KriptoBotu.exe --live
pause
