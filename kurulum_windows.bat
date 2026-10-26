@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Kripto Botu kurulumu ===
where py >nul 2>nul
if errorlevel 1 (
  echo Python bulunamadi. Lutfen once KURULUM.md dosyasindaki 1. adimi yapin.
  pause
  exit /b 1
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 veya daha yeni bir surum gerekli. KURULUM.md 1. adima bakin.
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Paketler yuklenemedi. Internet baglantinizi kontrol edin.
  pause
  exit /b 1
)
echo.
echo Testler calistiriliyor...
python -m pytest -q
echo.
echo Kurulum tamam. Botu baslatmak icin calistir_windows.bat dosyasina cift tiklayin.
pause
