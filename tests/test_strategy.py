from __future__ import annotations

from datetime import timedelta

import pytest

from cryptobot.config import ExitConfig, StrategyConfig
from cryptobot.domain.models import Candle, Symbol
from cryptobot.strategy.base import Direction, MarketState, PortfolioState, RiskState
from cryptobot.strategy.exits import ExitState, on_bar_close
from cryptobot.strategy.indicators import atr, ema, resample
from cryptobot.strategy.trend import TrendBreakoutStrategy
from tests.candles_util import H, T0, from_closes, random_walk

BTC = Symbol("BTC", "TRY")


def test_ema_is_seeded_with_sma_and_has_warmup() -> None:
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2
    assert out[3] == pytest.approx(3) and out[4] == pytest.approx(4)


def test_atr_uses_true_range_with_gaps() -> None:
    c = [Candle(T0 + i * H, 10, 11, 9, 10, 1) for i in range(3)]
    c.append(Candle(T0 + 3 * H, 14, 15, 13, 14, 1))  # gap up: true range 15 - 10 = 5
    out = atr(c, 2)
    assert out[2] == 2
    assert out[3] == pytest.approx((2 + 5) / 2)


def test_resample_only_counts_finished_big_bars() -> None:
    candles = from_closes([float(i) for i in range(1, 10)])  # 9 hourly bars from 00:00
    big, last_done = resample(candles, 60, 240)
    # bar 2 (02:00-03:00): the first 4h bar is not finished yet
    assert last_done[:4] == [-1, -1, -1, 0]
    assert last_done[4:8] == [0, 0, 0, 1]
    assert big[0].open == candles[0].open and big[0].close == candles[3].close
    assert big[0].high == max(c.high for c in candles[:4])


def test_resample_tolerates_missing_hours() -> None:
    candles = from_closes([1.0, 2.0, 3.0, 4.0, 5.0])
    gappy = [candles[0], candles[1], candles[4]]  # 02:00 and 03:00 missing
    big, last_done = resample(gappy, 60, 240)
    assert last_done == [-1, -1, 0]  # the 04:00 bar proves the first block is over
    assert big[0].close == 2.0


def test_no_look_ahead_full_history_equals_truncated_history() -> None:
    strat = TrendBreakoutStrategy(StrategyConfig())
    candles = random_walk(1500, seed=7)
    full = strat.prepare(BTC, candles)
    longs = 0
    for i in range(strat.warmup_bars, len(candles), 37):
        a = strat.signal_at(full, i)
        b = strat.signal_at(strat.prepare(BTC, candles[: i + 1]), i)
        assert (a.direction, a.invalidation_price, a.rationale) == (b.direction, b.invalidation_price, b.rationale)
        longs += a.is_entry
    for i in range(strat.warmup_bars, len(candles)):
        if strat.signal_at(full, i).is_entry:
            b = strat.signal_at(strat.prepare(BTC, candles[: i + 1]), i)
            assert b.is_entry
            longs += 1
    assert longs > 0


def _uptrend_breakout() -> list[Candle]:
    # Long steady rise, a pause under a ceiling, then a breakout close.
    closes = [1000 * 1.002**i for i in range(700)]
    top = closes[-1]
    closes += [top * (1 - 0.004 * (i % 3)) for i in range(25)]
    closes.append(top * 1.02)
    return from_closes(closes)


def test_breakout_in_uptrend_is_long_with_atr_stop() -> None:
    strat = TrendBreakoutStrategy(StrategyConfig())
    candles = _uptrend_breakout()
    sig = strat.generate_signal(MarketState(BTC, candles, candles[-1].ts), PortfolioState(), RiskState())
    assert sig.direction is Direction.LONG, sig.rationale
    a = sig.feature_snapshot["atr"]
    assert sig.invalidation_price == pytest.approx(candles[-1].close - 2.5 * a)
    assert 0.5 <= sig.confidence <= 0.9
    assert "trend" in sig.rationale and sig.strategy_version.startswith("trend_breakout")
    assert sig.bar_ts == candles[-1].ts


def test_downtrend_and_open_position_and_halts_are_flat() -> None:
    strat = TrendBreakoutStrategy(StrategyConfig())
    down = from_closes([1000 * 0.998**i for i in range(700)] + [1000 * 0.998**699 * 1.03])
    sig = strat.generate_signal(MarketState(BTC, down, down[-1].ts), PortfolioState(), RiskState())
    assert sig.direction is Direction.FLAT and "trend" in sig.rationale
    up = _uptrend_breakout()
    held = strat.generate_signal(MarketState(BTC, up, up[-1].ts), PortfolioState(frozenset({BTC})), RiskState())
    assert held.direction is Direction.FLAT
    halted = strat.generate_signal(MarketState(BTC, up, up[-1].ts), PortfolioState(), RiskState(False))
    assert halted.direction is Direction.FLAT


def test_not_enough_history_is_flat() -> None:
    strat = TrendBreakoutStrategy(StrategyConfig())
    short = from_closes([100.0 + i for i in range(50)])
    sig = strat.generate_signal(MarketState(BTC, short, short[-1].ts), PortfolioState(), RiskState())
    assert sig.direction is Direction.FLAT and "geçmiş" in sig.rationale


def test_trend_bar_must_be_multiple() -> None:
    with pytest.raises(ValueError):
        StrategyConfig(bar_minutes=120, trend_bar_minutes=60)


# ---------------------------------------------------------------- exits

CFG = ExitConfig()


def test_breakeven_then_trailing_stop_only_moves_up() -> None:
    st = ExitState(entry=100, initial_stop=95, stop=95, highest=100, bars_held=1)
    d = on_bar_close(CFG, st, 103, atr=2, trend_ok=True)
    assert d.exit_reason is None and not st.breakeven and st.stop == 95
    d = on_bar_close(CFG, st, 105, atr=2, trend_ok=True)  # +1R
    assert d.moved_to_breakeven and st.breakeven
    assert st.stop == pytest.approx(100.2)  # entry + 20 bps
    d = on_bar_close(CFG, st, 112, atr=2, trend_ok=True)
    assert st.stop == pytest.approx(106) and d.stop_moved and not d.moved_to_breakeven
    on_bar_close(CFG, st, 110, atr=2, trend_ok=True)
    assert st.stop == pytest.approx(106)  # never lowered
    d = on_bar_close(CFG, st, 105.5, atr=2, trend_ok=True)
    assert d.exit_reason == "satış"  # stop above entry: a profit, not a stop-loss


def test_close_below_first_stop_is_stop_loss() -> None:
    st = ExitState(entry=100, initial_stop=95, stop=95, highest=100, bars_held=1)
    assert on_bar_close(CFG, st, 94, atr=2, trend_ok=True).exit_reason == "stop-loss"


def test_time_and_trend_exits() -> None:
    st = ExitState(entry=100, initial_stop=95, stop=95, highest=100, bars_held=CFG.stale_after_bars)
    d = on_bar_close(CFG, st, 101, atr=2, trend_ok=True)
    assert d.exit_reason == "satış" and "ilerlemedi" in (d.note or "")
    st = ExitState(entry=100, initial_stop=95, stop=100.2, highest=110, bars_held=CFG.max_hold_bars,
                   breakeven=True)
    assert "süre" in (on_bar_close(CFG, st, 109, atr=1, trend_ok=True).note or "")
    st = ExitState(entry=100, initial_stop=95, stop=95, highest=100, bars_held=2)
    d = on_bar_close(CFG, st, 101, atr=2, trend_ok=False)
    assert d.exit_reason == "satış" and "trend" in (d.note or "")
    st = ExitState(entry=100, initial_stop=95, stop=95, highest=100, bars_held=2)
    assert on_bar_close(CFG, st, 101, atr=2, trend_ok=None).exit_reason is None


def test_warmup_covers_trend_ema() -> None:
    strat = TrendBreakoutStrategy(StrategyConfig())
    assert strat.warmup_bars >= 50 * 4
    assert strat.bar == timedelta(hours=1)
