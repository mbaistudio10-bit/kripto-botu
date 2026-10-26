"""Internal event log: one place every alert, halt and (later) trade is recorded.

Each event is appended as one JSON line to data/events.jsonl and handed to
subscribers (Telegram now; the phone page and Jarvis' read-only API later).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from cryptobot.fmt import fmt_num, fmt_try

log = logging.getLogger(__name__)


class Level(str, Enum):
    KRITIK = "KRITIK"  # needs attention now (limit hit, delist with open position)
    UYARI = "UYARI"  # something is off; bot protects itself
    BILGI = "BILGI"  # informational (recovered, started, command done)


@dataclass(frozen=True, slots=True)
class Event:
    level: Level
    kind: str  # machine name, e.g. "daily_loss_halt", "trade_closed"
    message: str  # Turkish, ready to show the user
    data: dict[str, Any] = field(default_factory=dict)
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Whether Telegram should push it. Default: KRITIK and UYARI only.
    notify: bool | None = None

    @property
    def should_notify(self) -> bool:
        return self.notify if self.notify is not None else self.level is not Level.BILGI

    @property
    def key(self) -> str:
        """Repeats of the same key are rate limited by the notifier."""
        return f"{self.kind}:{self.data.get('key', '')}"

    def to_json(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(timespec="seconds"),
            "level": self.level.value,
            "kind": self.kind,
            "message": self.message,
            "data": {k: str(v) if isinstance(v, Decimal) else v for k, v in self.data.items()},
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Event:
        return cls(
            level=Level(raw["level"]),
            kind=raw["kind"],
            message=raw["message"],
            data=raw.get("data", {}),
            time=datetime.fromisoformat(raw["time"]),
        )


Subscriber = Callable[[Event], None]


class EventLog:
    def __init__(self, path: Path | None, keep_in_memory: int = 200) -> None:
        self._path = path
        self._recent: deque[Event] = deque(maxlen=keep_in_memory)
        self._subscribers: list[Subscriber] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def emit(
        self,
        level: Level,
        kind: str,
        message: str,
        *,
        notify: bool | None = None,
        **data: Any,
    ) -> Event:
        event = Event(level, kind, message, data, notify=notify)
        self._recent.append(event)
        log.log(
            logging.WARNING if level is not Level.BILGI else logging.INFO,
            "event %s %s: %s",
            level.value,
            kind,
            message,
        )
        if self._path is not None:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
            except OSError as exc:  # a full disk must not stop risk checks
                log.error("cannot write event log: %s", exc)
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break the others
                log.exception("event subscriber failed")
        return event

    def recent(self, n: int = 20) -> list[Event]:
        return list(self._recent)[-n:]

    def read_since(self, since: datetime) -> Iterator[Event]:
        """All stored events at or after `since` (used for reports)."""
        if self._path is None or not self._path.exists():
            yield from (e for e in self._recent if e.time >= since)
            return
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = Event.from_json(json.loads(line))
                except (ValueError, KeyError):
                    continue
                if event.time >= since:
                    yield event


# ---------------------------------------------------------------------------
# Instant notifications. Paper trading calls these on every fill;
# Telegram (Achilles) pushes each one immediately, never rate limited.

TRADE_OPENED = "trade_opened"
TRADE_CLOSED = "trade_closed"
STOP_LOSS_HIT = "stop_loss_hit"
SYSTEM_ERROR = "system_error"
INSTANT_KINDS = frozenset({TRADE_OPENED, TRADE_CLOSED, STOP_LOSS_HIT})


def emit_trade_opened(
    events: EventLog, *, trade_id: str, symbol: str, quantity: Decimal, price: Decimal,
    value_try: Decimal, stop_price: Decimal | None, target_price: Decimal | None = None,
    paper: bool = True, note: str | None = None,
) -> Event:
    """note: why the bot bought, in Turkish (the strategy's rationale)."""
    tag = " (deneme)" if paper else ""
    stop = f"\nZarar-kes (stop): {fmt_num(stop_price)}" if stop_price is not None else ""
    target = f"\nHedef: {fmt_num(target_price)}" if target_price is not None else ""
    return events.emit(
        Level.BILGI, TRADE_OPENED,
        f"🛒 ALIŞ{tag}: {symbol}\nMiktar: {quantity.normalize():f} @ {fmt_num(price)}\n"
        f"Tutar: {fmt_try(value_try)}{stop}{target}" + (f"\nSebep: {note}" if note else ""),
        notify=True, key=trade_id, trade_id=trade_id, symbol=symbol, side="BUY",
        quantity=quantity, price=price, value_try=value_try, stop_price=stop_price,
        target_price=target_price, paper=paper,
    )


def emit_trade_closed(
    events: EventLog, *, trade_id: str, symbol: str, quantity: Decimal, price: Decimal,
    pnl_try: Decimal, pnl_usd: Decimal, reason: str = "satış", note: str | None = None,
    paper: bool = True,
) -> Event:
    """reason: 'satış' (normal exit), 'stop-loss' or 'acil kapatma'. note: why, in Turkish."""
    tag = " (deneme)" if paper else ""
    stop = reason == "stop-loss"
    head = f"🛑 STOP-LOSS ÇALIŞTI{tag}: {symbol}" if stop else f"💰 SATIŞ{tag}: {symbol}"
    sign = "+" if pnl_try >= 0 else ""
    why = f"\nSebep: {note}" if note else ""
    return events.emit(
        Level.UYARI if stop else Level.BILGI, STOP_LOSS_HIT if stop else TRADE_CLOSED,
        f"{head}\nMiktar: {quantity.normalize():f} @ {fmt_num(price)}\n"
        f"Sonuç: {sign}{fmt_try(pnl_try)} (≈ {pnl_usd:+.2f} USD){why}",
        notify=True, key=trade_id, trade_id=trade_id, symbol=symbol, side="SELL",
        quantity=quantity, price=price, pnl_try=pnl_try, pnl_usd=pnl_usd, reason=reason,
        paper=paper,
    )


class ErrorEventHandler(logging.Handler):
    """Turns ERROR log records into 'system_error' events, so they reach Telegram."""

    def __init__(self, events: EventLog) -> None:
        super().__init__(level=logging.ERROR)
        self._events = events
        self._busy = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._busy:  # an error while reporting an error must not loop
            return
        self._busy = True
        try:
            text = record.getMessage()[:300]
            if record.exc_info and record.exc_info[1] is not None:
                text += f" ({type(record.exc_info[1]).__name__})"
            self._events.emit(
                Level.UYARI, SYSTEM_ERROR, f"⚙️ Sistem hatası: {text}",
                key=record.name, logger=record.name,
            )
        finally:
            self._busy = False
