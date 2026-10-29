"""End-of-day summary: one short message per day for the owner and Jarvis.

At the configured local time (default 22:00 Turkey time) the bot writes a summary
of the 24 hours up to then (so nothing between two summaries is left out): profit, trades, coins, balance, open positions, protection and health.
It is sent to Telegram as one message, pushed to Jarvis (if a webhook is set; retried
until Jarvis is reachable) and stored in data/daily_summaries.jsonl, so Jarvis can
also fetch it later with GET /api/v1/daily-summary. The bot itself never depends on
Jarvis: if Jarvis is off, nothing else changes.

If the bot was off at summary time, the missing summary is written at the next start.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptobot.events import EventLog, Level
from cryptobot.fmt import fmt_num, fmt_try
from cryptobot.report import build_report

log = logging.getLogger(__name__)

DAILY_SUMMARY = "daily_summary"


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return Decimal(0)


def summary_window(day: date, at: time | None, utc_offset_hours: int) -> tuple[datetime, datetime]:
    """UTC [start, end) covered by the summary of `day`: the 24 hours up to `at` local time
    on that day, or the whole local calendar day when `at` is None."""
    offset = timedelta(hours=utc_offset_hours)
    end = datetime.combine(day, at or time(0), tzinfo=timezone.utc) - offset
    if at is None:
        end += timedelta(days=1)
    return end - timedelta(days=1), end


def build_summary(
    day: date, snap: dict[str, Any], events: EventLog, utc_offset_hours: int, at: time | None = None,
) -> dict[str, Any]:
    """Summary of `day` (see summary_window). `snap` is the status snapshot at the time of writing."""
    start, end = summary_window(day, at, utc_offset_hours)
    report = build_report((e for e in events.read_since(start) if e.time < end), "gün", start)
    pf = snap.get("portfolio") or {}
    risk = snap.get("risk") or {}
    health = snap.get("health") or {}
    guard = risk.get("capital_guard")

    lines = [f"📒 Kripto Botu gün sonu özeti ({day:%d.%m.%Y})"]
    sign = "+" if report.pnl_try >= 0 else ""
    if report.trades:
        coins = ", ".join(f"{c} {'+' if v >= 0 else ''}{fmt_try(v)}" for c, v in sorted(report.by_coin.items()))
        lines.append(f"Kazanç: {sign}{fmt_try(report.pnl_try)} (≈ {report.pnl_usd:+.2f} USD)")
        lines.append(f"İşlemler: {report.trades} (kârlı {report.wins}, zararlı {report.losses}) · {coins}")
    else:
        lines.append("Son 24 saatte kapanan işlem yok. (Strateji net bir yükseliş bekliyor; bu normaldir.)")
    if pf:
        lines.append(f"Bakiye: {fmt_try(_d(pf.get('equity_try')))} (≈ {fmt_num(_d(pf.get('equity_usd')))} USD), "
                     f"başlangıca göre {'+' if _d(pf.get('total_change_try')) >= 0 else ''}"
                     f"{fmt_try(_d(pf.get('total_change_try')))}")
        for pos in pf.get("positions", []):
            lines.append(f"Açık: {pos['pair']} {'+' if _d(pos['unrealized_try']) >= 0 else ''}"
                         f"{fmt_try(_d(pos['unrealized_try']))} (%{fmt_num(_d(pos['unrealized_pct']))})")
    if guard:
        lines.append(f"Ana para tabanı: {fmt_try(_d(guard['floor']))} (tabana {fmt_try(_d(guard['headroom']))} var)")
    if risk.get("halts"):
        lines.append("Durum: ⛔ " + " | ".join(h["message"] for h in risk["halts"]))
    else:
        lines.append("Durum: ✅ normal")
    if health:
        lines.append(f"Sistem sağlığı: {fmt_num(_d(health.get('score')), 1)} / 10 · {health.get('summary', '')}")
    lines.append("Deneme parasıdır, yatırım tavsiyesi değildir." if pf.get("paper", True)
                 else "Gerçek para hesabıdır. Yatırım tavsiyesi değildir.")

    return {
        "date": day.isoformat(),
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report": report.to_json(),
        "equity_try": pf.get("equity_try"),
        "equity_usd": pf.get("equity_usd"),
        "total_change_try": pf.get("total_change_try"),
        "open_positions": [p["pair"] for p in pf.get("positions", [])],
        "capital_floor_try": guard["floor"] if guard else None,
        "halts": [h["message"] for h in risk.get("halts", [])],
        "health_score": health.get("score"),
        "text": "\n".join(lines),
    }


class SummaryStore:
    """Summaries, one JSON line each; the newest line for a date wins."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        try:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                        out[item["date"]] = item
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        return out

    def get(self, day: str | None = None) -> dict[str, Any] | None:
        items = self.all()
        if not items:
            return None
        return items.get(day) if day else items[max(items)]

    def add(self, summary: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")


class DailySummaryTask:
    def __init__(
        self, snapshot: Callable[[], dict[str, Any]], events: EventLog, store: SummaryStore,
        at: time, utc_offset_hours: int, started_at: datetime,
    ) -> None:
        self._snapshot = snapshot
        self._events = events
        self._store = store
        self._at = at
        self._offset = timedelta(hours=utc_offset_hours)
        self._hours = utc_offset_hours
        self._started_at = started_at

    def _local_now(self, now: datetime) -> datetime:
        return now + self._offset

    def next_run(self, now: datetime) -> datetime:
        local = self._local_now(now)
        target = datetime.combine(local.date(), self._at, tzinfo=timezone.utc)
        if target <= local:
            target += timedelta(days=1)
        return target - self._offset

    def write(self, day: date) -> dict[str, Any]:
        summary = build_summary(day, self._snapshot(), self._events, self._hours, self._at)
        self._store.add(summary)
        self._events.emit(Level.BILGI, DAILY_SUMMARY, summary["text"], notify=True,
                          key=summary["date"], summary=summary)
        return summary

    def catch_up(self, now: datetime) -> dict[str, Any] | None:
        """Write the last due summary if the bot was off at summary time but ran in its 24 hours."""
        due = self.next_run(now) - timedelta(days=1)
        day = self._local_now(due).date()
        if self._store.get(day.isoformat()) is not None:
            return None
        start, end = summary_window(day, self._at, self._hours)
        ran = any(e.time < end for e in self._events.read_since(start))
        return self.write(day) if ran else None

    async def run(self) -> None:
        try:
            self.catch_up(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 - a summary must never stop the bot
            log.exception("daily summary catch-up failed")
        while True:
            now = datetime.now(timezone.utc)
            await asyncio.sleep(max(1.0, (self.next_run(now) - now).total_seconds()))
            try:
                self.write(self._local_now(datetime.now(timezone.utc)).date())
            except Exception:  # noqa: BLE001
                log.exception("daily summary failed")
            await asyncio.sleep(61)  # never twice for the same minute
