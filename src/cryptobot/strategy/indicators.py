"""Causal indicators: the value at index i only uses data up to and including i.

None marks the warm-up bars where an indicator has no value yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from cryptobot.domain.models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average, seeded with the simple average of the first `period` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    cur = sum(values[:period]) / period
    out[period - 1] = cur
    for i in range(period, len(values)):
        cur += alpha * (values[i] - cur)
        out[i] = cur
    return out


def atr(candles: Sequence[Candle], period: int) -> list[float | None]:
    """Average True Range (Wilder smoothing)."""
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = [candles[0].high - candles[0].low]
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    cur = sum(trs[1 : period + 1]) / period
    out[period] = cur
    for i in range(period + 1, len(candles)):
        cur = (cur * (period - 1) + trs[i]) / period
        out[i] = cur
    return out


def resample(candles: Sequence[Candle], bar_minutes: int, big_minutes: int) -> tuple[list[Candle], list[int]]:
    """Build bigger bars (e.g. 4h from 1h) and say which of them are finished at each small bar.

    Returns (big_bars, last_done) where last_done[i] is the index of the newest big bar
    that had fully closed when small bar i closed, or -1. A big bar is only "done" once
    the small bar that closes at its end has closed: no peeking into an unfinished bar.
    Gaps (hours without trades) are tolerated.
    """
    big: list[Candle] = []
    last_done: list[int] = []
    small, span = timedelta(minutes=bar_minutes), timedelta(minutes=big_minutes)
    epoch = datetime(1970, 1, 1, tzinfo=candles[0].ts.tzinfo) if candles else None
    cur: list[float] | None = None  # open, high, low, close, volume
    cur_start: datetime | None = None
    done = -1
    for c in candles:
        assert epoch is not None
        start = epoch + ((c.ts - epoch) // span) * span
        if cur_start is not None and start != cur_start:
            big.append(Candle(cur_start, *cur))  # type: ignore[misc]
            done = len(big) - 1
            cur = None
        if cur is None:
            cur, cur_start = [c.open, c.high, c.low, c.close, c.volume], start
        else:
            cur[1] = max(cur[1], c.high)
            cur[2] = min(cur[2], c.low)
            cur[3] = c.close
            cur[4] += c.volume
        if c.ts + small >= start + span:  # this small bar closes the big one
            big.append(Candle(cur_start, *cur))  # type: ignore[misc]
            done = len(big) - 1
            cur, cur_start = None, None
        last_done.append(done)
    return big, last_done
