from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cryptobot.config import Settings
from cryptobot.domain.models import MarketType, Symbol, TradingMode


def test_default_settings_match_user_spec(settings: Settings) -> None:
    assert settings.trading_mode is TradingMode.PAPER_TRADING
    assert settings.market_type is MarketType.SPOT
    assert settings.leverage == 1
    assert settings.pairs == [Symbol("BTC", "TRY"), Symbol("ETH", "TRY"), Symbol("USDT", "TRY")]
    assert settings.risk.max_concurrent_positions == 2
    assert settings.risk.max_daily_loss_pct == Decimal(3)
    assert settings.risk.max_weekly_loss_pct == Decimal(6)
    assert settings.risk.max_total_drawdown_pct == Decimal(10)
    assert settings.risk.max_risk_per_trade_pct == Decimal(1)
    assert settings.fx.max_age_seconds == 300
    assert settings.display_currency == "TRY"
    assert settings.internal_valuation_currency == "USD"


def _with(settings: Settings, **changes: object) -> dict:
    raw = settings.model_dump(mode="json")
    raw["pairs"] = [str(p) for p in settings.pairs]
    raw["fx"]["source_pair"] = str(settings.fx.source_pair)
    for key, value in changes.items():
        target = raw
        *path, last = key.split("__")
        for part in path:
            target = target[part]
        target[last] = value
    return raw


def test_roundtrip(settings: Settings) -> None:
    assert Settings.model_validate(_with(settings)) == settings


@pytest.mark.parametrize("mode", ["LIVE_TRADING", "BACKTEST", "KILL_SWITCH"])
def test_phase1_rejects_other_modes(settings: Settings, mode: str) -> None:
    with pytest.raises(ValidationError, match="not allowed in Phase 1"):
        Settings.model_validate(_with(settings, trading_mode=mode))


def test_spot_requires_1x(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="leverage 1"):
        Settings.model_validate(_with(settings, leverage=3))


def test_risk_limits_must_be_ordered(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="max_daily_loss_pct cannot exceed"):
        Settings.model_validate(_with(settings, risk__max_daily_loss_pct=8))


def test_unknown_key_is_rejected(settings: Settings) -> None:
    raw = _with(settings)
    raw["max_daily_los_pct"] = 3  # typo must not be silently ignored
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_pair_with_unsupported_asset(settings: Settings) -> None:
    with pytest.raises(ValidationError, match="supported_assets"):
        Settings.model_validate(_with(settings, pairs=["SOL/TRY"]))


def test_symbol_parse() -> None:
    assert Symbol.parse("btc-try") == Symbol("BTC", "TRY")
    assert str(Symbol.parse(" eth/usdt ")) == "ETH/USDT"
    with pytest.raises(ValueError):
        Symbol.parse("BTCTRY")
