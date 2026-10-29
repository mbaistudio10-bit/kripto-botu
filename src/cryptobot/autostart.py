"""Windows: start the bot hidden at every login (a small script in the Startup folder).

`KriptoBotu.exe --autostart on` writes the script and starts the bot in the background
right away; `--autostart off` removes it. The script is UTF-16 so folder names with
Turkish letters (e.g. "Masaüstü") work.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_NAME = "KriptoBotu.vbs"


def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_script(exe: Path) -> str:
    folder = str(exe.parent).replace('"', '""')
    target = str(exe).replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{folder}"\r\n'
        f'sh.Run """{target}"" --background", 0, False\r\n'
    )


def set_autostart(enabled: bool, exe: Path, folder: Path | None = None) -> Path:
    script = (folder or startup_folder()) / SCRIPT_NAME
    if enabled:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(launcher_script(exe), encoding="utf-16")  # with BOM: wscript reads it as Unicode
    else:
        script.unlink(missing_ok=True)
    return script


def autostart_command(value: str) -> int:
    if sys.platform != "win32":
        print("Otomatik başlatma şimdilik sadece Windows'ta var.")
        return 1
    exe = Path(sys.executable).resolve()
    script = set_autostart(value == "on", exe)
    if value == "on":
        os.startfile(script)  # type: ignore[attr-defined]  # start hidden right now
        print("Kripto Botu arka planda çalışmaya başladı ve Windows her açıldığında kendiliğinden başlayacak.")
        print("Takip: Kontrol Merkezi (tarayıcıda http://localhost:8770) veya Telegram /durum.")
    else:
        print("Otomatik başlatma kaldırıldı.")
    return 0
