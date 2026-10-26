"""Fixed rule sets compared by the backtest.

These values are chosen up front and are NOT tuned on the results: picking
settings until the past looks good would only fit the noise. A variant is
judged on the out-of-sample (unseen) period only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptobot.config import ExitConfig, StrategyConfig


@dataclass(frozen=True, slots=True)
class Variant:
    key: str
    label: str  # Turkish, shown in the report
    strategy: StrategyConfig
    exits: ExitConfig


# Slower versions of the same idea: fewer, bigger moves, so fees and the noise of
# thin TRY order books matter less. Time limits are kept at about the same days.
_SLOWER: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    ("trend_4h", "4 saatlik mum, günlük trend",
     {"bar_minutes": 240, "trend_bar_minutes": 1440},
     {"stale_after_bars": 18, "max_hold_bars": 90}),
    ("trend_1d", "Günlük mum, günlük trend (en yavaş)",
     {"bar_minutes": 1440, "trend_bar_minutes": 1440},
     {"stale_after_bars": 10, "max_hold_bars": 60}),
]


def variants(current: StrategyConfig, current_exits: ExitConfig, compare: bool = True) -> list[Variant]:
    """The owner's current settings first, then the fixed slower rule sets."""
    out = [Variant("current", f"{_bar_label(current)} (şu anki ayarlar)", current, current_exits)]
    if not compare:
        return out
    for key, label, strat, exits in _SLOWER:
        cfg = StrategyConfig(**{**StrategyConfig().model_dump(), "pairs": current.pairs, **strat})
        if (cfg.bar_minutes, cfg.trend_bar_minutes) == (current.bar_minutes, current.trend_bar_minutes):
            continue  # the owner already runs this one
        out.append(Variant(key, label, cfg, ExitConfig(**{**ExitConfig().model_dump(), **exits})))
    return out


def _bar_label(cfg: StrategyConfig) -> str:
    def name(m: int) -> str:
        return "günlük" if m == 1440 else f"{m // 60} saatlik" if m >= 60 else f"{m} dakikalık"

    return f"{name(cfg.bar_minutes).capitalize()} mum, {name(cfg.trend_bar_minutes)} trend"
