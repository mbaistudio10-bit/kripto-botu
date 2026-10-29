@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo CANLI TEST: cok kucuk (yaklasik 30 TL) GERCEK alis ve hemen satis yapilir.
echo Once botun arka planda calismadigindan emin olun (ArkaPlan-Durdur.bat).
KriptoBotu.exe --live-test
pause
