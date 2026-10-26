"""The ExchangeAdapter port. Every exchange (OKX, Binance, Bybit, ...) implements it.

The bot is read-only on purpose: this interface has no order methods at all,
so no code path can place a real order. Trading gets its own port later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from datetime import datetime

from cryptobot.domain.models import Candle, Instrument, MarketType, OrderBook, Symbol, Ticker


class ExchangeError(Exception):
    """Any failure talking to an exchange (network, bad reply, rate limit)."""


class ExchangeAdapter(ABC):
    name: str

    async def __aenter__(self) -> ExchangeAdapter:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def fetch_instruments(self, market_type: MarketType) -> list[Instrument]: ...

    @abstractmethod
    async def fetch_ticker(self, symbol: Symbol) -> Ticker: ...

    @abstractmethod
    async def fetch_order_book(self, symbol: Symbol, depth: int) -> OrderBook: ...

    @abstractmethod
    def stream_tickers(self, symbols: Sequence[Symbol]) -> AsyncIterator[Ticker]:
        """Yield tickers as they arrive. Reconnecting is the adapter's job."""

    @abstractmethod
    async def fetch_candles(
        self, symbol: Symbol, bar_minutes: int, before: datetime | None = None, limit: int = 100
    ) -> list[Candle]:
        """Closed candles, oldest first. `before`: only bars that opened before this time
        (None = the most recent ones). Unfinished bars are never returned."""
