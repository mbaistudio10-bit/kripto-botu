"""Strategy port: every strategy turns market, portfolio and risk state into a Signal.

Strategies never place orders. A LONG signal is only a proposal: the runner
sizes it with the RiskEngine and the RiskEngine can still refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from cryptobot.domain.models import Candle, Symbol


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: Symbol
    quote_currency: str
    direction: Direction
    confidence: float  # 0..1
    expected_entry: float | None
    invalidation_price: float | None  # the first stop
    target_zones: tuple[float, ...]
    time_horizon: str
    required_liquidity: float | None  # min book depth in quote currency, None = risk engine default
    max_allowed_slippage_bps: float | None
    rationale: str  # Turkish, shown to the owner
    feature_snapshot: dict[str, Any]
    strategy_version: str
    bar_ts: datetime | None = None  # open time of the candle the decision was made on

    @property
    def is_entry(self) -> bool:
        return self.direction is Direction.LONG


@dataclass(frozen=True, slots=True)
class MarketState:
    symbol: Symbol
    candles: list[Candle]  # closed candles only, oldest first
    now: datetime


@dataclass(frozen=True, slots=True)
class PortfolioState:
    open_symbols: frozenset[Symbol] = frozenset()


@dataclass(frozen=True, slots=True)
class RiskState:
    new_positions_allowed: bool = True
    reasons: list[str] = field(default_factory=list)


class Strategy(Protocol):
    name: str
    version: str

    def generate_signal(
        self, market_state: MarketState, portfolio_state: PortfolioState, risk_state: RiskState
    ) -> Signal: ...


def flat(symbol: Symbol, why: str, version: str, features: dict[str, Any] | None = None,
         bar_ts: datetime | None = None) -> Signal:
    return Signal(symbol, symbol.quote, Direction.FLAT, 0.0, None, None, (), "", None, None,
                  why, features or {}, version, bar_ts)
