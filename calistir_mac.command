#!/bin/bash
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Once kurulum_mac.command calistirin."; read -p "Enter'a basin..."; exit 1; }
source .venv/bin/activate
export PYTHONPATH=src
echo "Durdurmak icin Ctrl+C."
python -m cryptobot
read -p "Enter'a basin..."
