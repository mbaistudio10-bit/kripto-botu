"""Typed configuration loaded from config/settings.yaml."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cryptobot.domain.models import MarketType, Symbol, TradingMode

# Modes this phase is allowed to run in. Anything else is refused at startup.
PHASE1_ALLOWED_MODES = {TradingMode.READ_ONLY, TradingMode.PAPER_TRADING}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLimits(_Strict):
    max_concurrent_positions: int = Field(ge=1, le=20)
    max_daily_loss_pct: Decimal = Field(gt=0, le=100)
    max_weekly_loss_pct: Decimal = Field(gt=0, le=100)
    max_total_drawdown_pct: Decimal = Field(gt=0, le=100)
    max_risk_per_trade_pct: Decimal = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _consistent(self) -> RiskLimits:
        if self.max_risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError("max_risk_per_trade_pct cannot exceed max_daily_loss_pct")
        if self.max_daily_loss_pct > self.max_weekly_loss_pct:
            raise ValueError("max_daily_loss_pct cannot exceed max_weekly_loss_pct")
        if self.max_weekly_loss_pct > self.max_total_drawdown_pct:
            raise ValueError("max_weekly_loss_pct cannot exceed max_total_drawdown_pct")
        return self


class ExchangeConfig(_Strict):
    name: str
    rest_base_url: str
    ws_public_url: str
    request_timeout_seconds: float = Field(default=10, gt=0)


class MarketDataConfig(_Strict):
    mode: Literal["websocket", "rest"] = "websocket"
    rest_poll_interval_seconds: float = Field(default=3, ge=0.5)
    order_book_depth: int = Field(default=20, ge=1, le=400)
    order_book_refresh_seconds: float = Field(default=10, ge=1)
    stale_after_seconds: float = Field(default=30, gt=0)


class FxConfig(_Strict):
    source_pair: Symbol
    max_age_seconds: float = Field(default=300, gt=0)

    @field_validator("source_pair", mode="before")
    @classmethod
    def _parse(cls, v: object) -> Symbol:
        return Symbol.parse(v) if isinstance(v, str) else v  # type: ignore[return-value]


class LiquidityConfig(_Strict):
    max_spread_bps: Decimal = Field(gt=0)
    min_book_depth_try: Decimal = Field(ge=0)
    min_24h_volume_try: Decimal = Field(ge=0)


class StatusConfig(_Strict):
    print_every_seconds: float = Field(default=5, gt=0)
    status_file: Path = Path("data/status.json")
    log_file: Path = Path("logs/bot.log")


class Settings(_Strict):
    trading_mode: TradingMode
    market_type: MarketType
    leverage: Decimal = Field(ge=1)
    display_currency: str
    risk_currency: str
    internal_valuation_currency: str
    primary_quote_currencies: list[str]
    supported_assets: list[str]
    pairs: list[Symbol] = Field(min_length=1)
    starting_capital: Decimal = Field(gt=0)
    starting_capital_currency: str
    risk: RiskLimits
    exchange: ExchangeConfig
    market_data: MarketDataConfig
    fx: FxConfig
    liquidity: LiquidityConfig
    status: StatusConfig

    @field_validator("pairs", mode="before")
    @classmethod
    def _parse_pairs(cls, v: object) -> list[Symbol]:
        if not isinstance(v, list):
            raise ValueError("pairs must be a list like [BTC/TRY, ETH/TRY]")
        return [Symbol.parse(p) if isinstance(p, str) else p for p in v]

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.trading_mode not in PHASE1_ALLOWED_MODES:
            raise ValueError(
                f"trading_mode {self.trading_mode.value} is not allowed in Phase 1; "
                "use READ_ONLY or PAPER_TRADING"
            )
        if self.market_type is MarketType.SPOT and self.leverage != 1:
            raise ValueError("SPOT trading must use leverage 1")
        assets = set(self.supported_assets)
        for pair in self.pairs:
            if pair.quote not in self.primary_quote_currencies:
                raise ValueError(f"{pair} quote {pair.quote} is not in primary_quote_currencies")
            missing = {pair.base, pair.quote} - assets
            if missing:
                raise ValueError(f"{pair} uses assets not in supported_assets: {sorted(missing)}")
        for ccy in (self.display_currency, self.risk_currency, self.starting_capital_currency):
            if ccy not in assets:
                raise ValueError(f"currency {ccy} is not in supported_assets")
        return self


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is empty or not a YAML mapping")
    return Settings.model_validate(raw)
