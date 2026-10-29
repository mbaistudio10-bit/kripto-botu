from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cryptobot.config import Settings
from cryptobot.domain.models import MarketType, Symbol, TradingMode
from tests.conftest import ROOT


def test_default_settings_match_user_spec(settings: Settings) -> None:
    assert settings.trading_mode is TradingMode.PAPER_TRADING
    assert settings.market_type is MarketType.SPOT
    assert settings.leverage == 1
    assert settings.pairs == [Symbol("BTC", "USDT"), Symbol("ETH", "USDT"),
                              Symbol("BTC", "TRY"), Symbol("ETH", "TRY"), Symbol("USDT", "TRY")]
    assert settings.strategy.pairs == [Symbol("BTC", "USDT"), Symbol("ETH", "USDT")]
    assert settings.paper.cash_currency == "USDT"
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
def test_rejects_non_paper_modes(settings: Settings, mode: str) -> None:
    with pytest.raises(ValidationError, match="not allowed yet"):
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


def test_old_settings_with_test_trigger_still_load() -> None:
    import yaml

    from cryptobot.config import Settings

    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw.pop("strategy"), raw.pop("exits"), raw.pop("backtest")
    raw["test_trigger"] = {"enabled": True, "pairs": ["BTC/TRY"], "every_minutes": 30}
    s = Settings.model_validate(raw)
    assert s.strategy.enabled and [str(p) for p in s.strategy.pairs] == ["BTC/USDT", "ETH/USDT"]


def test_strategy_pairs_must_be_configured_pairs() -> None:
    import pytest
    import yaml

    from cryptobot.config import Settings

    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw["strategy"]["pairs"] = ["SOL/TRY"]
    with pytest.raises(ValueError):
        Settings.model_validate(raw)
