"""Backtest engine: replays closed candles through the SAME strategy and exit code
the live bot uses.

Realism rules:
  * No look-ahead: a decision made at a bar's close is filled at the NEXT bar's open.
    Indicators are causal (see strategy/indicators.py) and the trend timeframe only
    uses finished bars.
  * Costs: exchange fee on every buy and sell, plus slippage (worse price) per side.
  * Stops: if a bar's low touches the stop, the fill is the stop price, or the bar's
    open if it already gapped below it, minus slippage.
  * Risk rules mirror the live RiskEngine: risk per trade, max open positions,
    max share of the account per position, daily / weekly loss halts (Turkey time)
    and the drawdown emergency stop (closes everything, no more trading).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from bisect import bisect_right

from cryptobot.config import ExitConfig, PaperConfig, RiskLimits
from cryptobot.domain.models import Candle, Symbol
from cryptobot.strategy.base import Signal
from cryptobot.strategy.exits import ExitState, on_bar_close
from cryptobot.strategy.trend import TrendBreakoutStrategy

_MIN_ORDER_TRY = 50.0


@dataclass(frozen=True, slots=True)
class BtTrade:
    symbol: Symbol
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float  # in the quote currency (TRY), after all costs
    fees: float
    r_multiple: float  # pnl / money at risk at entry
    reason: str
    note: str


@dataclass
class BtResult:
    label: str
    start: datetime
    end: datetime
    start_equity: float
    end_equity: float
    trades: list[BtTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)
    buy_hold_pct: dict[Symbol, float] = field(default_factory=dict)
    exposure_pct: float = 0.0
    usd_start: float | None = None
    usd_end: float | None = None
    signals: int = 0
    skipped: int = 0  # signals not taken (limits, cash, position cap)


@dataclass(slots=True)
class _Pos:
    quantity: float
    entry_price: float
    cost: float  # cash paid incl. fee
    fees: float
    risk_money: float
    entry_ts: datetime
    exit: ExitState


class Backtester:
    def __init__(
        self, strategy: TrendBreakoutStrategy, exits: ExitConfig, limits: RiskLimits,
        paper: PaperConfig, slippage_bps: float, capital: float,
    ) -> None:
        self.strategy = strategy
        self.exits = exits
        self.limits = limits
        self.fee = float(paper.fee_bps) / 10_000
        self.max_pos_frac = float(paper.max_position_pct) / 100
        self.slip = slippage_bps / 10_000
        self.capital = capital

    def run(
        self, series: dict[Symbol, list[Candle]], start: datetime, end: datetime,
        label: str = "", fx: list[Candle] | None = None,
    ) -> BtResult:
        """Trade bars that open in [start, end). Bars before `start` only warm up indicators."""
        strat, lim = self.strategy, self.limits
        bar = strat.bar
        prepared = {s: strat.prepare(s, c) for s, c in series.items() if c}
        index = {s: {c.ts: i for i, c in enumerate(p.candles)} for s, p in prepared.items()}
        timeline = sorted({ts for idx in index.values() for ts in idx if start <= ts < end})
        res = BtResult(label, start, end, self.capital, self.capital)
        if not timeline:
            return res

        cash = self.capital
        positions: dict[Symbol, _Pos] = {}
        pending: dict[Symbol, Signal] = {}
        last_close: dict[Symbol, float] = {}
        offset = timedelta(hours=lim.reset_utc_offset_hours)
        day_key = week_key = None
        day_start = week_start = peak = self.capital
        daily_halt = weekly_halt = dd_halt = False
        bars_in_market = 0
        first_open: dict[Symbol, float] = {}

        def equity() -> float:
            return cash + sum(p.quantity * last_close[s] for s, p in positions.items())

        def close(sym: Symbol, price: float, ts: datetime, reason: str, note: str) -> None:
            nonlocal cash
            pos = positions.pop(sym)
            fee = pos.quantity * price * self.fee
            proceeds = pos.quantity * price - fee
            cash += proceeds
            pnl = proceeds - pos.cost
            res.trades.append(BtTrade(sym, pos.entry_ts, ts, pos.entry_price, price, pos.quantity, pnl,
                                      pos.fees + fee, pnl / pos.risk_money if pos.risk_money else 0.0,
                                      reason, note))

        def enter(sym: Symbol, sig: Signal, c: Candle) -> None:
            nonlocal cash
            assert sig.expected_entry is not None and sig.invalidation_price is not None
            if daily_halt or weekly_halt or dd_halt or len(positions) >= lim.max_concurrent_positions:
                res.skipped += 1
                return
            price = c.open * (1 + self.slip)
            stop = price - (sig.expected_entry - sig.invalidation_price)  # keep the ATR distance
            if stop <= 0:
                res.skipped += 1
                return
            eq = equity()
            per_unit = price - stop + price * float(lim.assumed_round_trip_cost_bps) / 10_000
            qty = min(eq * float(lim.max_risk_per_trade_pct) / 100 / per_unit,
                      eq * self.max_pos_frac / price,
                      cash * 0.98 / (price * (1 + self.fee)))
            if qty * price < _MIN_ORDER_TRY:
                res.skipped += 1
                return
            fee = qty * price * self.fee
            cash -= qty * price + fee
            positions[sym] = _Pos(qty, price, qty * price + fee, fee, qty * per_unit, c.ts,
                                  ExitState(price, stop, stop, price))

        for ts in timeline:
            here = [s for s in prepared if ts in index[s]]
            # 1) Fills at the open, then stops touched inside the bar.
            for sym in here:
                c = prepared[sym].candles[index[sym][ts]]
                first_open.setdefault(sym, c.open)
                if sym in pending:
                    enter(sym, pending.pop(sym), c)
                pos = positions.get(sym)
                if pos is not None and c.low <= pos.exit.stop:
                    fill = min(c.open, pos.exit.stop) * (1 - self.slip)
                    stop_loss = pos.exit.stop < pos.entry_price
                    close(sym, fill, ts, "stop-loss" if stop_loss else "satış",
                          "zarar-kes" if stop_loss else "iz süren stop")
                last_close[sym] = c.close
            if positions:
                bars_in_market += 1

            # 2) Risk limits on the value at the bar's close (Turkey time days/weeks).
            local = ts + bar + offset
            dk, wk = local.date(), local.isocalendar()[:2]
            eq = equity()
            if dk != day_key:
                day_key, day_start, daily_halt = dk, eq, False
            if wk != week_key:
                week_key, week_start, weekly_halt = wk, eq, False
            peak = max(peak, eq)
            if not daily_halt and (day_start - eq) / day_start * 100 >= float(lim.max_daily_loss_pct):
                daily_halt = True
                res.halts.append(f"{local:%d.%m.%Y}: günlük zarar limiti doldu")
            if not weekly_halt and (week_start - eq) / week_start * 100 >= float(lim.max_weekly_loss_pct):
                weekly_halt = True
                res.halts.append(f"{local:%d.%m.%Y}: haftalık zarar limiti doldu")
            if not dd_halt and (peak - eq) / peak * 100 >= float(lim.max_total_drawdown_pct):
                dd_halt = True
                res.halts.append(f"{local:%d.%m.%Y}: zirveden düşüş limiti, ACİL DURDURMA (test burada durur)")
                for sym in list(positions):
                    close(sym, last_close[sym] * (1 - self.slip), ts, "acil kapatma", "acil durdurma")
                pending.clear()

            # 3) Decisions at the bar's close: exits first, then new entries for the next open.
            for sym in here:
                p, i = prepared[sym], index[sym][ts]
                c = p.candles[i]
                pos = positions.get(sym)
                if pos is not None:
                    pos.exit.bars_held += 1
                    d = on_bar_close(self.exits, pos.exit, c.close, p.atr[i], strat.trend_ok(p, i))
                    if d.exit_reason is not None:
                        close(sym, c.close * (1 - self.slip), ts, d.exit_reason, d.note or "")
                    continue
                if dd_halt or ts + bar >= end:
                    continue
                sig = strat.signal_at(p, i)
                if sig.is_entry:
                    res.signals += 1
                    pending[sym] = sig
            res.equity_curve.append((ts + bar, equity()))

        # Close what is still open at the end so results are comparable.
        last_ts = timeline[-1] + bar
        for sym in list(positions):
            close(sym, last_close[sym] * (1 - self.slip), last_ts, "satış", "test dönemi bitti")
        res.end_equity = cash
        res.exposure_pct = bars_in_market / len(timeline) * 100
        for sym, o in first_open.items():
            res.buy_hold_pct[sym] = (last_close[sym] / o - 1) * 100
        if fx:
            r0, r1 = _rate_at(fx, start), _rate_at(fx, last_ts)
            if r0 and r1:
                res.usd_start, res.usd_end = self.capital / r0, res.end_equity / r1
        return res


def _rate_at(fx: list[Candle], ts: datetime) -> float | None:
    """USD/TRY close of the last bar that finished by `ts`."""
    keys = [c.ts for c in fx]
    i = bisect_right(keys, ts) - 1
    return fx[i].close if i >= 0 else (fx[0].open if fx else None)
