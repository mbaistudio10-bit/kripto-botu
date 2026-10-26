"""Exchange-agnostic domain types. Prices and sizes are always Decimal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER_TRADING = "PAPER_TRADING"
    LIVE_TRADING = "LIVE_TRADING"
    READ_ONLY = "READ_ONLY"
    KILL_SWITCH = "KILL_SWITCH"


class MarketType(str, Enum):
    SPOT = "SPOT"
    USDT_M_FUTURES = "USDT_M_FUTURES"
    COIN_M_FUTURES = "COIN_M_FUTURES"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A unified pair name such as BTC/TRY, independent of exchange spelling."""

    base: str
    quote: str

    @classmethod
    def parse(cls, text: str) -> Symbol:
        parts = text.strip().upper().replace("-", "/").split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"invalid pair {text!r}, expected format BASE/QUOTE")
        return cls(parts[0], parts[1])

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: Symbol
    exchange_symbol: str
    market_type: MarketType
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    is_live: bool


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: Symbol
    last: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    high_24h: Decimal
    low_24h: Decimal
    open_24h: Decimal
    volume_24h_base: Decimal
    volume_24h_quote: Decimal
    exchange_ts: datetime
    received_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        if self.mid == 0:
            return Decimal(0)
        return (self.ask - self.bid) / self.mid * Decimal(10_000)

    @property
    def change_24h_pct(self) -> Decimal:
        if self.open_24h == 0:
            return Decimal(0)
        return (self.last - self.open_24h) / self.open_24h * Decimal(100)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: Symbol
    bids: tuple[BookLevel, ...]  # best (highest) first
    asks: tuple[BookLevel, ...]  # best (lowest) first
    exchange_ts: datetime
    received_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def depth_quote(self, side: str, within_bps: Decimal | None = None) -> Decimal:
        """Total quote-currency value on one side, optionally only near the mid."""
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return Decimal(0)
        if within_bps is not None and self.bids and self.asks:
            mid = (self.bids[0].price + self.asks[0].price) / 2
            band = mid * within_bps / Decimal(10_000)
            levels = tuple(lv for lv in levels if abs(lv.price - mid) <= band)
        return sum((lv.price * lv.size for lv in levels), Decimal(0))

    @property
    def imbalance(self) -> Decimal:
        """(bid - ask) / (bid + ask) of quote depth; +1 all bids, -1 all asks."""
        bid, ask = self.depth_quote("bid"), self.depth_quote("ask")
        total = bid + ask
        return (bid - ask) / total if total else Decimal(0)
