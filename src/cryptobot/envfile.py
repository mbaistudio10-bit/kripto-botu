"""Reads secrets (Telegram token, later exchange keys) from a local .env file.

The file stays on the owner's computer, is git-ignored, and is never logged.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments; quotes optional. Real env vars win."""
    candidates = [path, path.with_name(path.name + ".txt")]  # Notepad likes to add .txt
    values: dict[str, str] = {}
    for candidate in candidates:
        if candidate.is_file():
            # utf-8-sig drops the BOM Notepad sometimes writes.
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    values[key.strip()] = value
            break
    for key in list(values) + ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def ensure_keys(path: Path, names: list[str]) -> list[str]:
    """Create random secrets for missing keys and append them to the .env file.

    Returns the names that were added. Existing values are never touched.
    """
    existing = load_env(path)
    missing = [n for n in names if not existing.get(n)]
    if not missing:
        return []
    target = path if path.exists() or not path.with_name(path.name + ".txt").exists() \
        else path.with_name(path.name + ".txt")
    lines = ["", "# Jarvis keys, created automatically by the bot. Give them only to Jarvis."]
    lines += [f"{n}={secrets.token_urlsafe(32)}" for n in missing]
    prefix = ""
    if target.exists():
        text = target.read_text(encoding="utf-8-sig")
        prefix = "" if text.endswith("\n") or not text else "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "\n".join(lines) + "\n")
    return missing


ENV_TEMPLATE = """\
# Kripto Botu gizli ayarlar. Bu dosyayi kimseyle paylasmayin.
# Telegram (Achilles): BotFather'dan aldiginiz anahtari esittirin sagina yapistirin.
TELEGRAM_BOT_TOKEN=
# Telegram kimlik numaraniz (chat id).
TELEGRAM_CHAT_ID=
# Jarvis botun olaylarini kendi adresine almak isterse (istege bagli):
JARVIS_WEBHOOK_URL=
"""


def ensure_env_file(path: Path) -> bool:
    """Create an empty template the first time, so the owner only fills in values."""
    if path.exists() or path.with_name(path.name + ".txt").exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ENV_TEMPLATE, encoding="utf-8")
    return True
