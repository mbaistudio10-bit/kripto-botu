from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

import cryptobot.market_data.candles as candles_mod

from cryptobot.backtest.engine import Backtester
from cryptobot.backtest.report import metrics, render
from cryptobot.backtest.run import run_backtest, split_point
from cryptobot.config import ExitConfig, Settings, StrategyConfig
from cryptobot.domain.models import Candle, Symbol
from cryptobot.market_data.candles import cache_path, load_history, read_cache
from cryptobot.strategy.base import Direction, Signal, flat
from cryptobot.strategy.trend import TrendBreakoutStrategy
from tests.candles_util import H, T0, from_closes, random_walk
from tests.conftest import FakeAdapter

BTC, ETH = Symbol("BTC", "TRY"), Symbol("ETH", "TRY")


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch) -> None:
    monkeypatch.setattr(candles_mod, "_PAUSE_SECONDS", 0)


@dataclass
class _Prep:
    symbol: Symbol
    candles: list[Candle]
    atr: list[float | None]


class StubStrategy:
    """Goes LONG at the given bar indexes with a fixed stop distance."""

    bar = H

    def __init__(self, at: set[int], stop_dist: float = 2.0, trend: bool = True) -> None:
        self.at, self.stop_dist, self.trend = at, stop_dist, trend

    def prepare(self, symbol: Symbol, candles: list[Candle]) -> _Prep:
        return _Prep(symbol, candles, [1.0] * len(candles))

    def trend_ok(self, p: _Prep, i: int) -> bool:
        return self.trend

    def signal_at(self, p: _Prep, i: int) -> Signal:
        c = p.candles[i]
        if i not in self.at:
            return flat(p.symbol, "-", "stub")
        return Signal(p.symbol, "TRY", Direction.LONG, 0.6, c.close, c.close - self.stop_dist, (), "", None,
                      None, "stub", {}, "stub", c.ts)


def bt(settings: Settings, strat, slippage: float = 10, exits: ExitConfig | None = None) -> Backtester:
    return Backtester(strat, exits or settings.exits, settings.risk, settings.paper, slippage, 10_000.0)


def test_entry_fills_at_next_open_with_costs_and_risk_sizing(settings: Settings) -> None:
    closes = [100.0] * 5 + [101.0] + [100.0] * 60
    candles = from_closes(closes, spread=0.0)
    res = bt(settings, StubStrategy({5}, stop_dist=2.0)).run({BTC: candles}, T0, T0 + 66 * H)
    t = res.trades[0]
    assert t.entry_ts == candles[6].ts  # decided on bar 5's close, filled at bar 6's open
    assert t.entry_price == pytest.approx(candles[6].open * 1.001)
    # Risk at the stop (incl. assumed 0.2% costs) is 1% of the account.
    p = t.entry_price
    expected = min(100 / (2.0 + p * 0.002), 10_000 * 0.45 / p, 10_000 * 0.98 / (p * 1.001))
    assert t.quantity == pytest.approx(expected)
    assert t.fees == pytest.approx(t.quantity * (t.entry_price + t.exit_price) * 0.001)
    # Flat market: the trade loses about fees + slippage.
    assert t.pnl < 0


def test_round_trip_on_flat_prices_costs_fee_and_slippage(settings: Settings) -> None:
    candles = from_closes([100.0] * 80, spread=0.0)
    exits = ExitConfig(stale_after_bars=3)
    res = bt(settings, StubStrategy({2}, stop_dist=5.0), slippage=10, exits=exits).run({BTC: candles}, T0, T0 + 80 * H)
    t = res.trades[0]
    assert "ilerlemedi" in t.note
    notional = t.quantity * 100
    assert -t.pnl == pytest.approx(notional * (0.001 + 0.001) + notional * 0.002, rel=0.01)
    no_cost = bt(settings, StubStrategy({2}, stop_dist=5.0), slippage=0, exits=exits).run({BTC: candles}, T0, T0 + 80 * H)
    assert no_cost.trades[0].pnl > t.pnl


def test_stop_gap_fills_at_open_not_at_stop(settings: Settings) -> None:
    closes = [100.0] * 4 + [100.0, 100.0, 90.0] + [90.0] * 5
    candles = from_closes(closes, spread=0.0)
    candles[6] = Candle(candles[6].ts, 90.0, 90.0, 89.0, 90.0, 1)  # opens far below the stop
    res = bt(settings, StubStrategy({3}, stop_dist=2.0), slippage=0).run({BTC: candles}, T0, T0 + 12 * H)
    t = res.trades[0]
    assert t.reason == "stop-loss" and t.exit_price == pytest.approx(90.0)
    assert t.r_multiple < -1  # a gap can lose more than 1 R


def test_max_two_positions_and_drawdown_halt(settings: Settings) -> None:
    sym3 = Symbol("SOL", "TRY")
    flat_c = from_closes([100.0] * 30, spread=0.0)
    res = bt(settings, StubStrategy({2}, stop_dist=5.0)).run({BTC: flat_c, ETH: flat_c, sym3: flat_c}, T0, T0 + 30 * H)
    assert len({t.symbol for t in res.trades}) == 2 and res.skipped == 1

    # Every bar a new loser: the drawdown emergency stop must end trading.
    crash = from_closes([100.0 * 0.97**i for i in range(200)], spread=0.0)
    res = bt(settings, StubStrategy(set(range(1, 200)), stop_dist=1.0)).run({BTC: crash}, T0, T0 + 200 * H)
    assert any("ACİL" in h for h in res.halts) or any("günlük" in h for h in res.halts)
    assert metrics(res).max_drawdown_pct < 15


def test_real_strategy_end_to_end_and_turkish_report(settings: Settings) -> None:
    strat = TrendBreakoutStrategy(settings.strategy)
    series = {BTC: random_walk(4000, seed=3), ETH: random_walk(4000, seed=4, start_price=120_000)}
    start = series[BTC][strat.warmup_bars].ts
    end = series[BTC][-1].ts + H
    split = split_point(start, end, 70, H)
    assert start < split < end and (split - start) % H == timedelta(0)
    b = bt(settings, strat)
    is_, oos = b.run(series, start, split, "Öğrenme"), b.run(series, split, end, "Sınama")
    assert all(start <= t.entry_ts < split for t in is_.trades)
    assert all(split <= t.entry_ts for t in oos.trades)
    assert is_.signals > 0
    text = render(is_, oos, ["başlık"])
    assert "GEÇMİŞ VERİ TESTİ" in text and "garanti etmez" in text and "Sınama" in text
    assert "alıp hiç satmasaydınız" in text


async def test_load_history_pages_back_and_uses_cache(tmp_path) -> None:
    candles = random_walk(450, seed=2)
    now = candles[-1].ts + H
    adapter = FakeAdapter({}, candles={"BTC/TRY": candles})
    got = await load_history(adapter, BTC, 60, candles[0].ts, tmp_path, now)
    assert [c.ts for c in got] == [c.ts for c in candles]
    assert adapter.candle_calls >= 5  # 100 per page
    assert len(read_cache(cache_path(tmp_path, BTC, 60))) == 450
    calls = adapter.candle_calls
    again = await load_history(adapter, BTC, 60, candles[100].ts, tmp_path, now)
    assert len(again) == 350 and adapter.candle_calls - calls <= 2


async def test_run_backtest_writes_report(settings: Settings, tmp_path) -> None:
    n = 3000
    data = {"BTC/TRY": random_walk(n, seed=5), "ETH/TRY": random_walk(n, seed=6, start_price=120_000),
            "USDT/TRY": random_walk(n, seed=8, vol=0.001, start_price=40.0)}
    now = data["BTC/TRY"][-1].ts + H
    s = settings.model_copy(update={"backtest": settings.backtest.model_copy(
        update={"cache_dir": tmp_path / "c", "report_file": tmp_path / "r.txt"})})
    code, text = await run_backtest(s, FakeAdapter({}, candles=data), days=100, now=now)
    assert code == 0, text
    assert (tmp_path / "r.txt").read_text(encoding="utf-8").startswith("📈")
    assert "Dolar bazında" in text


async def test_run_backtest_without_data(settings: Settings, tmp_path) -> None:
    s = settings.model_copy(update={"backtest": settings.backtest.model_copy(update={"cache_dir": tmp_path})})
    code, text = await run_backtest(s, FakeAdapter({}), days=60, now=datetime.now(timezone.utc))
    assert code == 3 and "yeterli" in text


def test_default_strategy_config_matches_settings_file(settings: Settings) -> None:
    assert settings.strategy == StrategyConfig()
    assert settings.exits == ExitConfig()
