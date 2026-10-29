@echo off
cd /d "%~dp0"
rem Kripto Botu'nu gizli (pencere olmadan) calistirir ve Windows her acildiginda kendiliginden baslatir.
KriptoBotu.exe --autostart on
echo.
echo Pencere gorunmez; bot arka planda calisir. Durdurmak icin: ArkaPlan-Durdur.bat
echo.
pause
