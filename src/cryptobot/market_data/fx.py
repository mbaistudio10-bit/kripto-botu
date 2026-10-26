"""FX Rate Service: USD/TRY from the exchange's USDT/TRY price, with a freshness rule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from cryptobot.config import FxConfig
from cryptobot.market_data.service import MarketDataService


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str  # USD
    quote: str  # TRY
    rate: Decimal
    as_of: datetime
    source: str


class FxRateService:
    """Treats USDT as USD. A real USD/TRY feed can replace this later."""

    def __init__(self, market_data: MarketDataService, config: FxConfig) -> None:
        self._md = market_data
        self._config = config

    def usd_try(self) -> FxRate | None:
        t = self._md.ticker(self._config.source_pair)
        if t is None or t.mid <= 0:
            return None
        return FxRate("USD", self._config.source_pair.quote, t.mid, t.received_ts, str(t.symbol))

    def is_fresh(self, now: datetime | None = None) -> bool:
        """False means: block new positions (spec rule, FX older than 5 minutes)."""
        fx = self.usd_try()
        if fx is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - fx.as_of).total_seconds()
        return age <= self._config.max_age_seconds

    def to_usd(self, amount: Decimal, currency: str) -> Decimal | None:
        if currency in ("USD", "USDT"):
            return amount
        fx = self.usd_try()
        if fx is None or currency != fx.quote:
            return None
        return amount / fx.rate
