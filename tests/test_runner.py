from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from cryptobot.config import ExitConfig, StrategyConfig
from cryptobot.domain.models import Candle, Symbol
from cryptobot.events import TRADE_CLOSED, TRADE_OPENED
from cryptobot.market_data.candles import CandleService
from cryptobot.strategy.runner import StrategyRunner
from cryptobot.strategy.trend import TrendBreakoutStrategy
from tests.candles_util import H, from_closes
from tests.conftest import FakeAdapter
from tests.test_paper import BTC, ETH, USDT, Rig, rig, set_price  # noqa: F401 - fixture

PRICE = 3_500_000.0


def breakout_closes() -> list[float]:
    closes = [1000 * 1.002**i for i in range(700)]
    top = closes[-1]
    closes += [top * (1 - 0.004 * (i % 3)) for i in range(25)]
    closes.append(top * 1.02)
    scale = PRICE / closes[-1]
    return [c * scale for c in closes]


def candles_ending(now: datetime, closes: list[float], ago: timedelta = timedelta(minutes=2)) -> list[Candle]:
    """Candles whose last bar closed `ago` before now."""
    start = now - ago - len(closes) * H
    return from_closes(closes, start=start)


def make_runner(r: Rig, exits: ExitConfig | None = None) -> tuple[StrategyRunner, CandleService]:
    cs = CandleService(FakeAdapter({}), [BTC, ETH], 60, 1000, 60)
    runner = StrategyRunner(r.trader, r.engine, cs, TrendBreakoutStrategy(StrategyConfig()),
                            StrategyConfig(), exits or ExitConfig(), r.events, [BTC, ETH, USDT])
    return runner, cs


def test_fresh_breakout_opens_paper_trade_through_risk_engine(rig: Rig) -> None:
    runner, cs = make_runner(rig)
    cs.set_candles(BTC, candles_ending(rig.now, breakout_closes()))
    runner.step(rig.now)
    assert BTC in rig.portfolio.positions
    opened = [e for e in rig.seen if e.kind == TRADE_OPENED][0]
    assert "Sebep:" in opened.message and "trend" in opened.message
    assert "signal" in rig.kinds()
    pos = rig.portfolio.positions[BTC]
    assert pos.initial_stop == pos.stop_price < pos.entry_price
    # Risk at the stop stays within 1% of the account.
    assert pos.quantity * (pos.entry_price - pos.stop_price) <= Decimal(100)
    runner.step(rig.now)  # same candle again: nothing new
    assert len([e for e in rig.seen if e.kind == TRADE_OPENED]) == 1
    assert runner.snapshot()["pairs"]["BTC/TRY"]["decision"] == "LONG"


def test_old_candle_is_not_chased(rig: Rig) -> None:
    runner, cs = make_runner(rig)
    cs.set_candles(BTC, candles_ending(rig.now, breakout_closes(), ago=timedelta(minutes=40)))
    runner.step(rig.now)
    assert not rig.portfolio.positions


def test_signal_refused_by_risk_engine_tells_owner(rig: Rig) -> None:
    set_price(rig.md, BTC, str(PRICE), rig.now, depth_try="1000")
    runner, cs = make_runner(rig)
    cs.set_candles(BTC, candles_ending(rig.now, breakout_closes()))
    runner.step(rig.now)
    assert not rig.portfolio.positions
    blocked = [e for e in rig.seen if e.kind == "entry_blocked"]
    assert len(blocked) == 1 and blocked[0].should_notify and "sığ" in blocked[0].message


def test_manual_stop_means_no_signal_and_no_spam(rig: Rig) -> None:
    rig.engine.stop("test")
    runner, cs = make_runner(rig)
    cs.set_candles(BTC, candles_ending(rig.now, breakout_closes()))
    runner.step(rig.now)
    assert not rig.portfolio.positions and "entry_blocked" not in rig.kinds()


def test_breakeven_moves_stop_and_survives_restart(rig: Rig) -> None:
    runner, cs = make_runner(rig)
    closes = breakout_closes()
    cs.set_candles(BTC, candles_ending(rig.now, closes))
    runner.step(rig.now)
    pos = rig.portfolio.positions[BTC]
    risk = float(pos.entry_price - pos.stop_price)
    later = rig.now + H
    up = float(pos.entry_price) + 1.5 * risk
    cs.set_candles(BTC, candles_ending(later, closes + [up]))
    set_price(rig.md, BTC, str(round(up)), later)
    set_price(rig.md, USDT, "35", later)
    runner.step(later)
    assert pos.breakeven and pos.stop_price > pos.entry_price
    assert "stop_breakeven" in rig.kinds()
    again = rig.new_portfolio()
    assert again.positions[BTC].breakeven and again.positions[BTC].stop_price == pos.stop_price


def test_time_exit_sells_at_bar_close(rig: Rig) -> None:
    runner, cs = make_runner(rig, ExitConfig(stale_after_bars=1))
    closes = breakout_closes()
    cs.set_candles(BTC, candles_ending(rig.now, closes))
    runner.step(rig.now)
    later = rig.now + H
    cs.set_candles(BTC, candles_ending(later, closes + [closes[-1] * 1.001]))
    set_price(rig.md, BTC, str(PRICE), later)
    set_price(rig.md, USDT, "35", later)
    runner.step(later)
    assert BTC not in rig.portfolio.positions
    assert "ilerlemedi" in [e for e in rig.seen if e.kind == TRADE_CLOSED][0].message
