#!/bin/bash
cd "$(dirname "$0")"
echo "=== Kripto Botu kurulumu ==="
if ! command -v python3 >/dev/null; then
  echo "Python bulunamadi. KURULUM.md 1. adima bakin."; read -p "Enter'a basin..."; exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 veya daha yeni gerekli. KURULUM.md 1. adima bakin."; read -p "Enter'a basin..."; exit 1
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || { echo "Paketler yuklenemedi."; read -p "Enter'a basin..."; exit 1; }
echo; echo "Testler calistiriliyor..."; python -m pytest -q
echo; echo "Kurulum tamam. Botu baslatmak icin calistir_mac.command dosyasina cift tiklayin."
read -p "Enter'a basin..."
