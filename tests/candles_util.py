from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from cryptobot.domain.models import Candle

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)


def from_closes(closes: list[float], start: datetime = T0, spread: float = 0.002) -> list[Candle]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(start + i * H, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c, 1.0))
        prev = c
    return out


def random_walk(n: int, seed: int = 1, drift: float = 0.0, vol: float = 0.01,
                start_price: float = 3_000_000.0) -> list[Candle]:
    rnd = random.Random(seed)
    closes, p = [], start_price
    for i in range(n):
        # Slow regime changes so there are trends to follow and chop to avoid.
        p *= math.exp(drift + 0.004 * math.sin(i / 150) + rnd.gauss(0, vol))
        closes.append(p)
    return from_closes(closes)
