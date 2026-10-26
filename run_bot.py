"""Entry point for the packaged KriptoBotu.exe (PyInstaller)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The .exe looks for config/, data/ and logs/ next to itself.
if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).resolve().parent)

from cryptobot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    code = main()
    if getattr(sys, "frozen", False):
        input("\nKapatmak için Enter'a basın...")
    sys.exit(code)
