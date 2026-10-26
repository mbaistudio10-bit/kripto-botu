from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cryptobot.config import Settings, load_settings
from cryptobot.domain.models import BookLevel, Instrument, MarketType, OrderBook, Symbol, Ticker
from cryptobot.exchanges.base import ExchangeAdapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings() -> Settings:
    return load_settings(ROOT / "config" / "settings.yaml")


def make_ticker(pair: str, last: str, bid: str | None = None, ask: str | None = None) -> Ticker:
    d = Decimal(last)
    return Ticker(
        symbol=Symbol.parse(pair),
        last=d,
        bid=Decimal(bid) if bid else d,
        ask=Decimal(ask) if ask else d,
        bid_size=Decimal(1),
        ask_size=Decimal(1),
        high_24h=d,
        low_24h=d,
        open_24h=d,
        volume_24h_base=Decimal(100),
        volume_24h_quote=d * 100,
        exchange_ts=datetime.now(timezone.utc),
    )


class FakeAdapter(ExchangeAdapter):
    name = "fake"

    def __init__(self, prices: dict[str, str], listed: dict[str, bool] | None = None) -> None:
        self.prices = prices
        self.listed = listed if listed is not None else {p: True for p in prices}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_instruments(self, market_type: MarketType) -> list[Instrument]:
        return [
            Instrument(Symbol.parse(p), p.replace("/", "-"), market_type,
                       Decimal("0.1"), Decimal("0.0001"), Decimal("0.0001"), live)
            for p, live in self.listed.items()
        ]

    async def fetch_ticker(self, symbol: Symbol) -> Ticker:
        return make_ticker(str(symbol), self.prices[str(symbol)])

    async def fetch_order_book(self, symbol: Symbol, depth: int) -> OrderBook:
        p = Decimal(self.prices[str(symbol)])
        return OrderBook(
            symbol,
            bids=(BookLevel(p - 1, Decimal(2)),),
            asks=(BookLevel(p + 1, Decimal(1)),),
            exchange_ts=datetime.now(timezone.utc),
        )

    async def stream_tickers(self, symbols: Sequence[Symbol]) -> AsyncIterator[Ticker]:
        for s in symbols:
            yield await self.fetch_ticker(s)
