"""Candle history: download from the exchange (with a local CSV cache) and keep it fresh."""

from __future__ import annotations

import asyncio
import csv
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptobot.domain.models import Candle, Symbol
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError

log = logging.getLogger(__name__)

# OKX allows about 20 history requests per 2 seconds; stay well below.
_PAUSE_SECONDS = 0.15


def cache_path(cache_dir: Path, symbol: Symbol, bar_minutes: int) -> Path:
    return cache_dir / f"{symbol.base}-{symbol.quote}_{bar_minutes}m.csv"


def read_cache(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    out = []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or row[0] == "ts":
                    continue
                ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
                out.append(Candle(ts, *(float(x) for x in row[1:6])))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("candle cache %s unreadable (%s), downloading again", path, exc)
        return []
    return out


def write_cache(path: Path, candles: Sequence[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([int(c.ts.timestamp() * 1000), c.open, c.high, c.low, c.close, c.volume])
    tmp.replace(path)


def merge(*parts: Sequence[Candle]) -> list[Candle]:
    by_ts: dict[datetime, Candle] = {}
    for part in parts:
        for c in part:
            by_ts[c.ts] = c
    return [by_ts[k] for k in sorted(by_ts)]


async def _fetch_back(
    adapter: ExchangeAdapter, symbol: Symbol, bar_minutes: int, before: datetime | None,
    stop_at: datetime, max_pages: int,
) -> list[Candle]:
    """Page backwards from `before` (None = now) until bars reach `stop_at`."""
    got: list[Candle] = []
    cursor = before
    for _ in range(max_pages):
        rows = await adapter.fetch_candles(symbol, bar_minutes, before=cursor, limit=100)
        if not rows:
            break
        got.extend(rows)
        if rows[0].ts <= stop_at or (cursor is not None and rows[0].ts >= cursor):
            break
        cursor = rows[0].ts
        await asyncio.sleep(_PAUSE_SECONDS)
    return got


async def load_history(
    adapter: ExchangeAdapter, symbol: Symbol, bar_minutes: int, start: datetime,
    cache_dir: Path | None = None, now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[Candle]:
    """Closed candles from `start` until now, oldest first. Reuses and updates the cache."""
    now = now or datetime.now(timezone.utc)
    bar = timedelta(minutes=bar_minutes)
    path = cache_path(cache_dir, symbol, bar_minutes) if cache_dir else None
    cached = read_cache(path) if path else []
    pages = int((now - start) / bar / 100) + 5
    parts: list[list[Candle]] = [cached]
    if progress:
        progress(f"{symbol}: geçmiş fiyatlar indiriliyor...")
    newest = cached[-1].ts if cached else start
    parts.append(await _fetch_back(adapter, symbol, bar_minutes, None, newest, pages))
    if cached and cached[0].ts > start + bar:
        parts.append(await _fetch_back(adapter, symbol, bar_minutes, cached[0].ts, start, pages))
    candles = merge(*parts)
    if path and candles:
        try:
            write_cache(path, candles)
        except OSError as exc:
            log.warning("cannot write candle cache %s: %s", path, exc)
    return [c for c in candles if c.ts >= start]


class CandleService:
    """Keeps the latest closed candles per pair for the live strategy."""

    def __init__(
        self, adapter: ExchangeAdapter, pairs: Sequence[Symbol], bar_minutes: int,
        history_bars: int, refresh_seconds: float, cache_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._pairs = list(pairs)
        self.bar_minutes = bar_minutes
        self._history = history_bars
        self._refresh = refresh_seconds
        self._cache_dir = cache_dir
        self._candles: dict[Symbol, list[Candle]] = {}
        self._loaded: set[Symbol] = set()
        self.errors = 0
        self.last_error: str | None = None

    def candles(self, symbol: Symbol) -> list[Candle]:
        return self._candles.get(symbol, [])

    def set_candles(self, symbol: Symbol, candles: Sequence[Candle]) -> None:
        self._candles[symbol] = list(candles)[-self._history:]

    def _record_error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = str(exc)
        log.warning("candle data error: %s", exc)

    async def _initial(self, symbol: Symbol) -> None:
        start = datetime.now(timezone.utc) - timedelta(minutes=self.bar_minutes * self._history)
        self.set_candles(symbol, await load_history(self._adapter, symbol, self.bar_minutes, start,
                                                    self._cache_dir))

    async def run(self) -> None:
        while True:
            for symbol in self._pairs:
                try:
                    if symbol not in self._loaded:
                        await self._initial(symbol)
                        self._loaded.add(symbol)
                    else:
                        fresh = await self._adapter.fetch_candles(symbol, self.bar_minutes, limit=10)
                        self.set_candles(symbol, merge(self.candles(symbol), fresh))
                except ExchangeError as exc:
                    self._record_error(exc)
            await asyncio.sleep(self._refresh)
