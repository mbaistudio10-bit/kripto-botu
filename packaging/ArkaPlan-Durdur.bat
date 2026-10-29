@echo off
cd /d "%~dp0"
rem Arka planda calisan Kripto Botu'nu durdurur ve otomatik baslatmayi kaldirir.
KriptoBotu.exe --autostart off
taskkill /IM KriptoBotu.exe /F >nul 2>nul
echo.
echo Kripto Botu durduruldu. Tekrar baslatmak icin: ArkaPlanda-Calistir.bat veya KriptoBotu.exe
echo.
pause
