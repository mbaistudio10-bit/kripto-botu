"""Market Data Ingestion Service: keeps the latest ticker and order book per pair."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptobot.config import MarketDataConfig
from cryptobot.domain.models import MarketType, OrderBook, Symbol, Ticker
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PairCheck:
    available: list[Symbol]
    missing: list[Symbol]
    suspended: list[Symbol]


async def check_pairs(
    adapter: ExchangeAdapter, pairs: Sequence[Symbol], market_type: MarketType
) -> PairCheck:
    """Ask the exchange which of our pairs exist and are trading right now."""
    instruments = {i.symbol: i for i in await adapter.fetch_instruments(market_type)}
    available, missing, suspended = [], [], []
    for pair in pairs:
        inst = instruments.get(pair)
        if inst is None:
            missing.append(pair)
        elif not inst.is_live:
            suspended.append(pair)
        else:
            available.append(pair)
    return PairCheck(available, missing, suspended)


class MarketDataService:
    def __init__(
        self, adapter: ExchangeAdapter, pairs: Sequence[Symbol], config: MarketDataConfig
    ) -> None:
        self._adapter = adapter
        self._pairs = list(pairs)
        self._config = config
        self._tickers: dict[Symbol, Ticker] = {}
        self._books: dict[Symbol, OrderBook] = {}
        self.errors = 0
        self.last_error: str | None = None

    @property
    def pairs(self) -> list[Symbol]:
        return list(self._pairs)

    def ticker(self, symbol: Symbol) -> Ticker | None:
        return self._tickers.get(symbol)

    def book(self, symbol: Symbol) -> OrderBook | None:
        return self._books.get(symbol)

    def age_seconds(self, symbol: Symbol, now: datetime | None = None) -> float | None:
        t = self._tickers.get(symbol)
        if t is None:
            return None
        return ((now or datetime.now(timezone.utc)) - t.received_ts).total_seconds()

    def is_stale(self, symbol: Symbol, now: datetime | None = None) -> bool:
        age = self.age_seconds(symbol, now)
        return age is None or age > self._config.stale_after_seconds

    def on_ticker(self, ticker: Ticker) -> None:
        if ticker.symbol in self._pairs:
            self._tickers[ticker.symbol] = ticker

    def _record_error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = str(exc)
        log.warning("market data error: %s", exc)

    async def run(self) -> None:
        """Run forever. Cancel the task to stop."""
        ticker_task = (
            self._stream_tickers() if self._config.mode == "websocket" else self._poll_tickers()
        )
        await asyncio.gather(ticker_task, self._refresh_books())

    async def _stream_tickers(self) -> None:
        async for ticker in self._adapter.stream_tickers(self._pairs):
            self.on_ticker(ticker)

    async def _poll_tickers(self) -> None:
        while True:
            for pair in self._pairs:
                try:
                    self.on_ticker(await self._adapter.fetch_ticker(pair))
                except ExchangeError as exc:
                    self._record_error(exc)
            await asyncio.sleep(self._config.rest_poll_interval_seconds)

    async def _refresh_books(self) -> None:
        while True:
            for pair in self._pairs:
                try:
                    book = await self._adapter.fetch_order_book(pair, self._config.order_book_depth)
                    self._books[pair] = book
                except ExchangeError as exc:
                    self._record_error(exc)
            await asyncio.sleep(self._config.order_book_refresh_seconds)
