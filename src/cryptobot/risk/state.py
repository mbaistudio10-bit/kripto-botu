"""Risk state that must survive a restart (crash-only: a restart never clears a halt)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


class RiskStateError(Exception):
    """The saved risk state exists but cannot be read."""


@dataclass
class RiskState:
    peak_equity: Decimal | None = None
    day_key: str = ""
    day_start_equity: Decimal | None = None
    week_key: str = ""
    week_start_equity: Decimal | None = None
    manual_stop: str | None = None  # reason text while stopped by hand
    manual_stop_by: str | None = None  # "owner" or "jarvis"
    daily_halt_day: str | None = None
    weekly_halt_week: str | None = None
    drawdown_halt: bool = False
    delist_halts: list[str] = field(default_factory=list)
    approved_limits: dict[str, str] | None = None
    guard_base: Decimal | None = None  # starting capital in TRY for the capital floor
    guard_peak: Decimal | None = None  # best equity since guard_base was set
    floor_halt: bool = False

    def to_json(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> RiskState:
        state = cls()
        for key in ("peak_equity", "day_start_equity", "week_start_equity", "guard_base", "guard_peak"):
            if raw.get(key) is not None:
                setattr(state, key, Decimal(raw[key]))
        for key in ("day_key", "week_key"):
            setattr(state, key, str(raw.get(key, "")))
        state.manual_stop = raw.get("manual_stop")
        state.manual_stop_by = raw.get("manual_stop_by")
        state.daily_halt_day = raw.get("daily_halt_day")
        state.weekly_halt_week = raw.get("weekly_halt_week")
        state.drawdown_halt = bool(raw.get("drawdown_halt", False))
        state.floor_halt = bool(raw.get("floor_halt", False))
        state.delist_halts = list(raw.get("delist_halts", []))
        state.approved_limits = raw.get("approved_limits")
        return state


def load_state(path: Path) -> RiskState:
    if not path.exists():
        return RiskState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        return RiskState.from_json(raw)
    except (OSError, ValueError, ArithmeticError) as exc:
        raise RiskStateError(f"{path}: {exc}") from exc


def save_state(path: Path, state: RiskState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
