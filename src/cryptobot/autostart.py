"""Windows: start the bot in the background at every login.

`KriptoBotu.exe --autostart on` adds the bot to the user's normal startup list
(HKCU\\...\\CurrentVersion\\Run, the same place ordinary apps use) and starts it in the
background right away; `--autostart off` removes it. Earlier versions put a small
script in the Startup folder instead; antivirus programs treat that pattern as
suspicious, so it is removed when found.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "KriptoBotu"
OLD_SCRIPT_NAME = "KriptoBotu.vbs"


class StartupList(Protocol):
    def set(self, name: str, command: str) -> None: ...

    def delete(self, name: str) -> None: ...


class RunKey:
    """The current user's Run key in the Windows registry."""

    def set(self, name: str, command: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_command(exe: Path) -> str:
    return f'"{exe}" --background'


def set_autostart(enabled: bool, exe: Path, startup: StartupList, old_folder: Path | None = None) -> None:
    (old_folder or startup_folder()).joinpath(OLD_SCRIPT_NAME).unlink(missing_ok=True)
    if enabled:
        startup.set(VALUE_NAME, startup_command(exe))
    else:
        startup.delete(VALUE_NAME)


def start_in_background(exe: Path) -> None:
    """Start a copy with no window that keeps running after this one exits."""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe), "--background"], cwd=exe.parent, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hide_console() -> None:
    """--background on Windows: let go of the console window (it closes if it was ours alone)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a visible window is not worth failing over
        return
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def autostart_command(value: str) -> int:
    if sys.platform != "win32":
        print("Otomatik başlatma şimdilik sadece Windows'ta var.")
        return 1
    exe = Path(sys.executable).resolve()
    set_autostart(value == "on", exe, RunKey())
    if value == "on":
        start_in_background(exe)
        print("Kripto Botu arka planda çalışmaya başladı ve Windows her açıldığında kendiliğinden başlayacak.")
        print("Birkaç dakika içinde Telegram'a 'Kripto Botu başladı' mesajı gelir.")
        print("Takip: Telegram /durum veya Kontrol Merkezi (tarayıcıda http://localhost:8770).")
    else:
        print("Otomatik başlatma kaldırıldı.")
    return 0
