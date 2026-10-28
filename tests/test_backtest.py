from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

import cryptobot.market_data.candles as candles_mod

from cryptobot.backtest.engine import Backtester
from cryptobot.backtest.report import comparison_verdict, metrics, render_details
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
    b = Backtester(StubStrategy(set(range(1, 200)), stop_dist=1.0), settings.exits, settings.risk,
                   settings.paper, 10, 10_000.0, drawdown_pause_days=0)
    res = b.run({BTC: crash}, T0, T0 + 200 * H)
    assert any("ACİL" in h for h in res.halts) or any("günlük" in h for h in res.halts)
    assert metrics(res).max_drawdown_pct < 15


def test_after_drawdown_halt_trading_resumes_after_pause(settings: Settings) -> None:
    # Steady losers for a week, then the market goes flat: trading must restart after the pause.
    n = 24 * 30
    closes = [100.0 * 0.995**i for i in range(24 * 8)] + [100.0 * 0.995 ** (24 * 8)] * (n - 24 * 8)
    candles = from_closes(closes, spread=0.0)
    strat = StubStrategy(set(range(1, n, 6)), stop_dist=0.3)
    res = Backtester(strat, settings.exits, settings.risk, settings.paper, 10, 10_000.0,
                     drawdown_pause_days=7).run({BTC: candles}, T0, T0 + n * H)
    assert res.drawdown_halts >= 1
    halt_at = next(i for i, h in enumerate(res.halts) if "ACİL" in h)
    assert any("devam edildi" in h for h in res.halts[halt_at:])
    assert max(t.entry_ts for t in res.trades) > T0 + timedelta(days=15)
    stopped = Backtester(strat, settings.exits, settings.risk, settings.paper, 10, 10_000.0,
                         drawdown_pause_days=0).run({BTC: candles}, T0, T0 + n * H)
    assert len(stopped.trades) < len(res.trades)


def test_variants_are_fixed_and_start_with_current_settings(settings: Settings) -> None:
    from cryptobot.backtest.variants import variants

    vs = variants(settings.strategy, settings.exits)
    # The owner runs 4-hour candles now, so that fixed set is not repeated.
    assert [v.key for v in vs] == ["current", "trend_1h", "trend_1d"]
    assert vs[0].strategy == settings.strategy and "4 saatlik mum, günlük trend" in vs[0].label
    assert (vs[1].strategy.bar_minutes, vs[1].strategy.trend_bar_minutes) == (60, 240)
    assert (vs[1].exits.stale_after_bars, vs[1].exits.max_hold_bars) == (48, 240)
    assert (vs[2].strategy.bar_minutes, vs[2].strategy.trend_bar_minutes) == (1440, 1440)
    assert len(variants(settings.strategy, settings.exits, compare=False)) == 1
    hourly = StrategyConfig(bar_minutes=60, trend_bar_minutes=240)
    assert [v.key for v in variants(hourly, settings.exits)] == ["current", "trend_4h", "trend_1d"]


def test_daily_variant_runs_on_resampled_candles(settings: Settings) -> None:
    from cryptobot.backtest.variants import variants
    from cryptobot.strategy.indicators import resample

    v = variants(settings.strategy, settings.exits)[2]
    strat = TrendBreakoutStrategy(v.strategy)
    hourly = random_walk(24 * 400, seed=9, vol=0.006)
    daily = resample(hourly, 60, 1440)[0]
    assert len(daily) == 400 and daily[1].ts - daily[0].ts == timedelta(days=1)
    start = daily[strat.warmup_bars].ts
    res = Backtester(strat, v.exits, settings.risk, settings.paper, 10, 10_000.0).run(
        {BTC: daily}, start, daily[-1].ts + timedelta(days=1))
    assert res.equity_curve and all(t.entry_ts >= start for t in res.trades)


def test_verdict_needs_unseen_profit_and_enough_trades(settings: Settings) -> None:
    from cryptobot.backtest.engine import BtResult, BtTrade
    from cryptobot.backtest.variants import variants

    v = variants(settings.strategy, settings.exits)[0]

    def result(ret: float, n: int, win: bool) -> BtResult:
        r = BtResult("x", T0, T0 + H, 10_000.0, 10_000.0 * (1 + ret))
        r.trades = [BtTrade(BTC, T0, T0, 1, 1, 1, 10.0 if (win or i % 3) else -40.0, 0, 0, "", "")
                    for i in range(n)]
        return r

    assert "Hiçbir kural" in comparison_verdict([(v, result(0.05, 30, True), result(-0.02, 30, False))])
    assert "az" in comparison_verdict([(v, result(0.05, 30, True), result(0.03, 5, True))])
    assert "her iki dönemde" in comparison_verdict([(v, result(0.05, 30, True), result(0.03, 20, True))])


def test_real_strategy_end_to_end_and_turkish_report(settings: Settings) -> None:
    strat = TrendBreakoutStrategy(StrategyConfig(bar_minutes=60, trend_bar_minutes=240))
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
    text = render_details(is_, oos)
    assert "Öğrenme" in text and "Sınama" in text
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
            "USDT/TRY": random_walk(n, seed=8, vol=0.001, start_price=40.0),
            "BTC/USDT": random_walk(n, seed=5, start_price=90_000),
            "ETH/USDT": random_walk(n, seed=6, start_price=3_000)}
    now = data["BTC/TRY"][-1].ts + H
    s = settings.model_copy(update={"backtest": settings.backtest.model_copy(
        update={"cache_dir": tmp_path / "c", "report_file": tmp_path / "r.txt"})})
    code, text = await run_backtest(s, FakeAdapter({}, candles=data), days=100, now=now)
    assert code == 0, text
    full = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert full.startswith("📈") and "AYRINTILAR" in full and "Dolar bazında" in full
    assert "kural karşılaştırması" in text and "şu anki ayarlar" in text
    assert "Günlük mum" in text and "4 saatlik mum" in text and "1 saatlik mum" in text
    rows = [line for line in text.splitlines() if line[:2] in {f"{n})" for n in range(1, 10)}]
    # The bot's dollar pairs first, then every rule set also on the TL pairs.
    assert [r.endswith("dolar çiftleri (BTC/USDT, ETH/USDT)") for r in rows] == [True, False] * 3
    assert all(r.endswith("TL çiftleri (BTC/TRY, ETH/TRY)") for r in rows[1::2])
    assert "Hesap dolar (USDT) olarak tutuldu" in full and "TL bazında" in full
    assert "sadece dolar (USDT) tutmak" in text and "garanti etmez" in text
    assert len(text) < 4000  # one Telegram message


async def test_run_backtest_without_data(settings: Settings, tmp_path) -> None:
    s = settings.model_copy(update={"backtest": settings.backtest.model_copy(update={"cache_dir": tmp_path})})
    code, text = await run_backtest(s, FakeAdapter({}), days=60, now=datetime.now(timezone.utc))
    assert code == 3 and "yeterli" in text


def test_default_strategy_config_matches_settings_file(settings: Settings) -> None:
    assert settings.strategy == StrategyConfig()
    assert settings.exits == ExitConfig()


def test_usdt_account_keeps_idle_money_in_dollars(settings: Settings) -> None:
    btc_usdt = Symbol("BTC", "USDT")
    n = 48
    flat_c = from_closes([100.0] * n, spread=0.0)
    fx = from_closes([40.0 + 4.0 * i / (n - 1) for i in range(n)], spread=0.0)  # USD/TRY 40 -> 44
    end = T0 + n * H
    usd = Backtester(StubStrategy(set()), settings.exits, settings.risk, settings.paper, 10, 10_000.0,
                     quote="USDT").run({btc_usdt: flat_c}, T0, end, fx=fx)
    tl = bt(settings, StubStrategy(set())).run({BTC: flat_c}, T0, end, fx=fx)
    # No trades: dollars keep their value, so the TL value rises with the dollar.
    assert usd.quote == "USDT" and usd.start_equity == pytest.approx(10_000 / 40)
    assert metrics(usd).usd_return_pct == pytest.approx(0)
    assert metrics(usd).total_return_pct == pytest.approx(10)
    # Idle TL stays TL: worth less in dollars.
    assert metrics(tl).total_return_pct == pytest.approx(0)
    assert metrics(tl).usd_return_pct == pytest.approx((40 / 44 - 1) * 100)
    # Trades on a dollar pair are sized in USDT with the same risk rules.
    traded = Backtester(StubStrategy({2}, stop_dist=5.0), settings.exits, settings.risk, settings.paper, 10,
                        10_000.0, quote="USDT").run({btc_usdt: flat_c}, T0, end, fx=fx)
    assert traded.trades and traded.trades[0].quantity * 100 <= 250 * 0.45 + 1e-6
    text = render_details(usd, traded)
    assert "Hesap dolar (USDT) olarak tutuldu" in text and "USDT" in text
    with pytest.raises(ValueError):
        Backtester(StubStrategy(set()), settings.exits, settings.risk, settings.paper, 10, 10_000.0,
                   quote="USDT").run({btc_usdt: flat_c}, T0, end)


def test_dollar_account_must_make_dollars_to_pass(settings: Settings) -> None:
    from cryptobot.backtest.engine import BtResult, BtTrade
    from cryptobot.backtest.variants import variants

    v = variants(settings.strategy, settings.exits)[0]

    def result(usd_ret: float) -> BtResult:
        # The dollar rose 20% against TL, so TL looks good even when dollars were lost.
        r = BtResult("x", T0, T0 + H, 250.0, 250.0 * (1 + usd_ret), "USDT", usd_start=250.0,
                     usd_end=250.0 * (1 + usd_ret), tl_start=10_000.0, tl_end=10_000.0 * (1 + usd_ret) * 1.2)
        r.trades = [BtTrade(BTC, T0, T0, 1, 1, 1, 1.0 if i % 3 else -0.5, 0, 0, "", "") for i in range(20)]
        return r

    assert "Hiçbir kural" in comparison_verdict([(v, result(-0.05), result(-0.05))])
    assert "her iki dönemde" in comparison_verdict([(v, result(0.05), result(0.05))])


def test_tl_account_must_beat_holding_dollars(settings: Settings) -> None:
    from cryptobot.backtest.engine import BtResult, BtTrade
    from cryptobot.backtest.variants import variants

    v = variants(settings.strategy, settings.exits)[0]
    # +10% in TL, but the dollar rose 20% against TL: holding USDT would have been better.
    r = BtResult("x", T0, T0 + H, 10_000.0, 11_000.0, usd_start=250.0, usd_end=250.0 * 1.1 / 1.2)
    r.trades = [BtTrade(BTC, T0, T0, 1, 1, 1, 1.0 if i % 3 else -0.5, 0, 0, "", "") for i in range(20)]
    assert "Hiçbir kural" in comparison_verdict([(v, r, r)])
