"""Short profit / coins / trades summary, shaped for the owner's Telegram "X report".

Trades are read from the event log ("trade_closed" / "stop_loss_hit" events,
emitted by the paper trader with data: symbol, pnl_try, pnl_usd). Jarvis' read-only API (Phase 5) serves the same dict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from cryptobot.events import STOP_LOSS_HIT, TRADE_CLOSED, Event
from cryptobot.fmt import fmt_try

@dataclass
class Report:
    period: str  # "gün", "hafta", "ay"
    since: datetime
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_try: Decimal = Decimal(0)
    pnl_usd: Decimal = Decimal(0)
    by_coin: dict[str, Decimal] = field(default_factory=dict)  # coin -> pnl in TRY

    def to_json(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "since": self.since.isoformat(timespec="seconds"),
            "categories": {
                "Kazanç": {"pnl_try": f"{self.pnl_try:.2f}", "pnl_usd": f"{self.pnl_usd:.2f}"},
                "Kripto birimleri": {k: f"{v:.2f}" for k, v in sorted(self.by_coin.items())},
                "Al-sat işlemleri": {"total": self.trades, "wins": self.wins, "losses": self.losses},
            },
            "text": self.text(),
        }

    def text(self) -> str:
        if self.trades == 0:
            return f"Kripto Botu ({self.period}): henüz kapanan işlem yok. Kazanç: 0,00 TL."
        sign = "+" if self.pnl_try >= 0 else ""
        coins = ", ".join(
            f"{coin} {'+' if v >= 0 else ''}{fmt_try(v)}" for coin, v in sorted(self.by_coin.items())
        )
        return (
            f"Kripto Botu ({self.period})\n"
            f"Kazanç: {sign}{fmt_try(self.pnl_try)} (≈ {self.pnl_usd:+.2f} USD)\n"
            f"Kripto birimleri: {coins}\n"
            f"Al-sat işlemleri: {self.trades} (kârlı {self.wins}, zararlı {self.losses})"
        )


def build_report(events: Iterable[Event], period: str, since: datetime) -> Report:
    report = Report(period=period, since=since)
    for e in events:
        if e.kind not in (TRADE_CLOSED, STOP_LOSS_HIT) or e.time < since:
            continue
        pnl = Decimal(str(e.data.get("pnl_try", "0")))
        report.trades += 1
        report.wins += pnl > 0
        report.losses += pnl < 0
        report.pnl_try += pnl
        report.pnl_usd += Decimal(str(e.data.get("pnl_usd", "0")))
        coin = str(e.data.get("symbol", "?")).split("/")[0]
        report.by_coin[coin] = report.by_coin.get(coin, Decimal(0)) + pnl
    return report


def period_start(now: datetime, period: str, utc_offset_hours: int) -> datetime:
    """Start of today / this week (Monday) / this month in local time, as UTC."""
    offset = timedelta(hours=utc_offset_hours)
    local = (now + offset).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "hafta":
        local -= timedelta(days=local.weekday())
    elif period == "ay":
        local = local.replace(day=1)
    return local - offset
