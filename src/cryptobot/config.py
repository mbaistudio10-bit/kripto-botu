"""Typed configuration loaded from config/settings.yaml."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cryptobot.domain.models import MarketType, Symbol, TradingMode

# Modes the bot may run in today. LIVE_TRADING stays off until a later phase.
ALLOWED_MODES = {TradingMode.READ_ONLY, TradingMode.PAPER_TRADING}

# Fields of RiskLimits where a bigger number means more risk.
RISK_LIMIT_FIELDS = (
    "max_concurrent_positions",
    "max_daily_loss_pct",
    "max_weekly_loss_pct",
    "max_total_drawdown_pct",
    "max_risk_per_trade_pct",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLimits(_Strict):
    max_concurrent_positions: int = Field(ge=1, le=20)
    max_daily_loss_pct: Decimal = Field(gt=0, le=100)
    max_weekly_loss_pct: Decimal = Field(gt=0, le=100)
    max_total_drawdown_pct: Decimal = Field(gt=0, le=100)
    max_risk_per_trade_pct: Decimal = Field(gt=0, le=100)
    # Daily/weekly limits reset at local midnight / Monday. Turkey is UTC+3 all year.
    reset_utc_offset_hours: int = Field(default=3, ge=-12, le=14)
    # Fees + slippage assumed for one round trip, added to the stop distance.
    assumed_round_trip_cost_bps: Decimal = Field(default=Decimal(20), ge=0)
    # Send an early warning when a loss limit is this much used up (0.7 = 70%).
    warn_at_fraction: Decimal = Field(default=Decimal("0.7"), gt=0, lt=1)

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
    # Order book depth is counted within this distance of the mid price.
    depth_band_bps: Decimal = Field(default=Decimal(50), gt=0)
    # A pair must stay unfit this long before a Telegram warning is sent.
    alert_after_seconds: float = Field(default=120, ge=0)


class TelegramConfig(_Strict):
    enabled: bool = True
    # Holds TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Never commit it.
    env_file: Path = Path(".env")
    api_base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: int = Field(default=25, ge=1, le=50)
    # The same warning is not repeated more often than this. KRITIK always goes out.
    alert_cooldown_minutes: float = Field(default=30, ge=0)
    # The same kind of system error is repeated at most this often.
    error_cooldown_minutes: float = Field(default=5, ge=0)


class JarvisConfig(_Strict):
    """Local API for the owner's Jarvis assistant: read status + emergency stop/resume."""

    enabled: bool = True
    # 127.0.0.1 = only programs on this computer can connect.
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class PaperConfig(_Strict):
    """Simulated account: fills walk the real order book, nothing is sent to the exchange."""

    state_file: Path = Path("data/paper_state.json")
    # Exchange fee charged on every simulated buy and sell (10 = 0.10%, OKX spot taker).
    fee_bps: Decimal = Field(default=Decimal(10), ge=0)
    # One position may use at most this share of the account value.
    max_position_pct: Decimal = Field(default=Decimal(45), gt=0, le=100)
    # How often open positions are checked against their stop and target.
    check_every_seconds: float = Field(default=2, gt=0)
    # Idle money is kept in this currency. Money in any other currency (the
    # TL starting capital, the proceeds of an old TRY-pair trade) is changed
    # into it at the live rate, paying the fee once.
    cash_currency: str = "USDT"


def _symbols(v: object) -> list[Symbol]:
    if not isinstance(v, list):
        raise ValueError("pairs must be a list like [BTC/TRY, ETH/TRY]")
    return [Symbol.parse(p) if isinstance(p, str) else p for p in v]


class StrategyConfig(_Strict):
    """Trend breakout: buy when a pair breaks its recent high while the bigger
    (higher-timeframe) trend is up. Decisions use closed candles only."""

    enabled: bool = True
    pairs: list[Symbol] = Field(default_factory=lambda: [Symbol("BTC", "USDT"), Symbol("ETH", "USDT")])
    # 4-hour candles with a daily trend: the steadiest of the three rule sets
    # compared in the backtest (2026-10). 1-hour candles traded too often.
    bar_minutes: Literal[15, 30, 60, 120, 240, 1440] = 240
    # The trend filter looks at bars this long, built from the entry bars.
    trend_bar_minutes: Literal[60, 120, 240, 1440] = 1440
    trend_ema_fast: int = Field(default=20, ge=2)
    trend_ema_slow: int = Field(default=50, ge=3)
    ema_period: int = Field(default=50, ge=2)  # entry-timeframe trend line
    breakout_bars: int = Field(default=20, ge=2)  # close above the high of this many bars
    atr_period: int = Field(default=14, ge=2)
    stop_atr: float = Field(default=2.5, gt=0)  # first stop = entry - stop_atr x ATR
    # Skip pairs that barely move (fees eat the profit) or move wildly.
    min_atr_pct: float = Field(default=0.3, ge=0)
    max_atr_pct: float = Field(default=6, gt=0)
    history_bars: int = Field(default=1000, ge=100, le=5000)  # bars loaded at start
    refresh_seconds: float = Field(default=60, ge=10)
    # A signal is only acted on this soon after its candle closed.
    act_within_minutes: float = Field(default=10, gt=0)

    @field_validator("pairs", mode="before")
    @classmethod
    def _parse_pairs(cls, v: object) -> list[Symbol]:
        return _symbols(v)

    @model_validator(mode="after")
    def _check(self) -> StrategyConfig:
        if self.trend_bar_minutes % self.bar_minutes or self.trend_bar_minutes < self.bar_minutes:
            raise ValueError("trend_bar_minutes must be a multiple of bar_minutes")
        if self.trend_ema_fast >= self.trend_ema_slow:
            raise ValueError("trend_ema_fast must be smaller than trend_ema_slow")
        if self.min_atr_pct >= self.max_atr_pct:
            raise ValueError("min_atr_pct must be smaller than max_atr_pct")
        return self


class ExitConfig(_Strict):
    """Exit rules, independent of the entry strategy."""

    # Move the stop to entry (+ costs) once price is this many R above entry
    # (R = distance between entry and the first stop).
    breakeven_at_r: float = Field(default=1.0, gt=0)
    breakeven_buffer_bps: float = Field(default=20, ge=0)
    # After break-even the stop follows the highest close at this many ATR.
    trail_atr: float = Field(default=3.0, gt=0)
    # Sell a trade that never reached break-even after this many bars...
    stale_after_bars: int = Field(default=18, ge=1)  # 3 days of 4-hour candles
    # ...and every trade after this many bars.
    max_hold_bars: int = Field(default=90, ge=1)  # 15 days
    # Sell when the higher-timeframe trend turns down.
    exit_on_trend_break: bool = True


class BacktestConfig(_Strict):
    days: int = Field(default=730, ge=30, le=3650)
    # The first part is the learning period, the rest is the unseen test period.
    in_sample_pct: float = Field(default=70, ge=10, le=90)
    slippage_bps: float = Field(default=10, ge=0)  # per buy and per sell
    # After a drawdown emergency stop, act as if the owner typed "/devam ONAY"
    # after this many days (0 = stop trading for the rest of the period).
    drawdown_pause_days: float = Field(default=7, ge=0)
    # Also test the fixed slower rule sets (backtest/variants.py) side by side.
    compare_variants: bool = True
    # Also run every rule set on these pairs, priced in the other currency
    # (TL pairs when the bot trades dollar pairs), to compare where idle money
    # does better. [] = off. Results are shown in TL and in USD.
    compare_pairs: list[Symbol] = Field(
        default_factory=lambda: [Symbol("BTC", "TRY"), Symbol("ETH", "TRY")])
    cache_dir: Path = Path("data/candles")
    report_file: Path = Path("data/backtest_report.txt")

    @field_validator("compare_pairs", mode="before")
    @classmethod
    def _parse_compare_pairs(cls, v: object) -> list[Symbol]:
        return [] if v in (None, []) else _symbols(v)

    @model_validator(mode="after")
    def _check(self) -> BacktestConfig:
        if len({p.quote for p in self.compare_pairs} | {"TRY", "USDT"}) > 2:
            raise ValueError("compare_pairs must be priced in TRY or USDT, like [BTC/TRY, ETH/TRY]")
        return self


class StatusConfig(_Strict):
    print_every_seconds: float = Field(default=5, gt=0)
    status_file: Path = Path("data/status.json")
    log_file: Path = Path("logs/bot.log")
    events_file: Path = Path("data/events.jsonl")
    risk_state_file: Path = Path("data/risk_state.json")
    # Startup grace: missing/old prices are not alerted during the first seconds.
    alert_grace_seconds: float = Field(default=60, ge=0)
    listing_check_minutes: float = Field(default=5, gt=0)


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
    paper: PaperConfig = PaperConfig()
    strategy: StrategyConfig = StrategyConfig()
    exits: ExitConfig = ExitConfig()
    backtest: BacktestConfig = BacktestConfig()
    # Phase 3 placeholder, replaced by `strategy`. Old settings files still load.
    test_trigger: dict | None = None
    telegram: TelegramConfig = TelegramConfig()
    jarvis: JarvisConfig = JarvisConfig()

    @field_validator("pairs", mode="before")
    @classmethod
    def _parse_pairs(cls, v: object) -> list[Symbol]:
        return _symbols(v)

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.trading_mode not in ALLOWED_MODES:
            raise ValueError(
                f"trading_mode {self.trading_mode.value} is not allowed yet; "
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
        unknown = [str(p) for p in self.strategy.pairs if p not in self.pairs]
        if unknown:
            raise ValueError(f"strategy pairs must also be in pairs: {unknown}")
        if len({p.quote for p in self.backtest.compare_pairs}) > 1:
            raise ValueError("backtest.compare_pairs must all have the same quote currency")
        wrong_quote = [str(p) for p in self.strategy.pairs if p.quote != self.paper.cash_currency]
        if wrong_quote:
            raise ValueError(
                f"strategy pairs must be priced in paper.cash_currency "
                f"{self.paper.cash_currency}: {wrong_quote}"
            )
        for ccy in (self.display_currency, self.risk_currency, self.starting_capital_currency,
                    self.paper.cash_currency):
            if ccy not in assets:
                raise ValueError(f"currency {ccy} is not in supported_assets")
        return self


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is empty or not a YAML mapping")
    return Settings.model_validate(raw)
