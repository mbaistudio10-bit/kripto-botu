"""Trend breakout strategy with a higher-timeframe trend filter.

Buy (LONG) on a closed entry bar when ALL of these hold:
  1. Big picture is up: on the trend timeframe (default 4h) the last FINISHED bar
     closed above its slow EMA, and the fast EMA is above the slow EMA.
  2. Entry timeframe (default 1h) closes above its own EMA.
  3. Breakout: the close is above the highest high of the previous `breakout_bars` bars.
  4. Volatility is usable: ATR as % of price is between min_atr_pct and max_atr_pct.
The first stop (invalidation) is `stop_atr` x ATR below the close. Exits live in exits.py.

`prepare` computes every indicator once for a candle list; `signal_at(i)` only reads
values at or before i. The backtest and the live runner use exactly this code, so a
decision in the backtest is the decision the live bot would have made at that bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from cryptobot.config import StrategyConfig
from cryptobot.domain.models import Candle, Symbol
from cryptobot.strategy.base import (
    Direction, MarketState, PortfolioState, RiskState, Signal, flat,
)
from cryptobot.strategy.indicators import atr, ema, resample


@dataclass(frozen=True, slots=True)
class Prepared:
    symbol: Symbol
    candles: list[Candle]
    ema: list[float | None]
    atr: list[float | None]
    htf_close: list[float | None]  # per entry bar: close of the last finished trend bar
    htf_fast: list[float | None]
    htf_slow: list[float | None]


def bar_name(minutes: int) -> str:
    """Turkish name of a candle length: "günlük", "4 saatlik", "15 dakikalık"."""
    if minutes == 1440:
        return "günlük"
    return f"{minutes // 60} saatlik" if minutes >= 60 else f"{minutes} dakikalık"


class TrendBreakoutStrategy:
    name = "trend_breakout"
    version = "trend_breakout-1.0"

    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config

    @property
    def warmup_bars(self) -> int:
        """Bars needed before the first decision (trend EMA needs its slow period of big bars)."""
        ratio = self.cfg.trend_bar_minutes // self.cfg.bar_minutes
        return max((self.cfg.trend_ema_slow + 1) * ratio, self.cfg.ema_period,
                   self.cfg.breakout_bars, self.cfg.atr_period) + 1

    @property
    def bar(self) -> timedelta:
        return timedelta(minutes=self.cfg.bar_minutes)

    def prepare(self, symbol: Symbol, candles: list[Candle]) -> Prepared:
        cfg = self.cfg
        closes = [c.close for c in candles]
        big, last_done = resample(candles, cfg.bar_minutes, cfg.trend_bar_minutes) if candles else ([], [])
        big_closes = [b.close for b in big]
        fast, slow = ema(big_closes, cfg.trend_ema_fast), ema(big_closes, cfg.trend_ema_slow)

        def at(series: list[float | None]) -> list[float | None]:
            return [series[j] if j >= 0 else None for j in last_done]

        return Prepared(
            symbol, list(candles), ema(closes, cfg.ema_period), atr(candles, cfg.atr_period),
            at(big_closes), at(fast), at(slow),  # type: ignore[arg-type]
        )

    def trend_ok(self, p: Prepared, i: int) -> bool | None:
        """Is the higher-timeframe trend still up at bar i? None = not enough data."""
        close, slow = p.htf_close[i], p.htf_slow[i]
        if close is None or slow is None:
            return None
        return close > slow

    def signal_at(self, p: Prepared, i: int) -> Signal:
        cfg, sym, v = self.cfg, p.symbol, self.version
        c = p.candles[i]
        e, a = p.ema[i], p.atr[i]
        hc, hf, hs = p.htf_close[i], p.htf_fast[i], p.htf_slow[i]
        if i < cfg.breakout_bars or None in (e, a, hc, hf, hs):
            return flat(sym, "yeterli geçmiş veri yok", v, bar_ts=c.ts)
        assert e is not None and a is not None and hc is not None and hf is not None and hs is not None
        level = max(x.high for x in p.candles[i - cfg.breakout_bars : i])
        atr_pct = a / c.close * 100
        features = {
            "close": c.close, "ema": round(e, 6), "atr": round(a, 6), "atr_pct": round(atr_pct, 3),
            "breakout_level": level, "htf_close": hc, "htf_ema_fast": round(hf, 6),
            "htf_ema_slow": round(hs, 6),
        }
        trend_name = bar_name(cfg.trend_bar_minutes)
        if not (hc > hs and hf > hs):
            return flat(sym, f"büyük resimde ({trend_name}) yükseliş trendi yok", v, features, c.ts)
        if c.close <= e:
            return flat(sym, "fiyat ortalamanın altında", v, features, c.ts)
        if c.close <= level:
            return flat(sym, f"son {cfg.breakout_bars} mumun zirvesi aşılmadı", v, features, c.ts)
        if not cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct:
            return flat(sym, f"oynaklık uygun değil (%{atr_pct:.2f})", v, features, c.ts)
        stop = c.close - cfg.stop_atr * a
        # Stronger breakouts (in ATRs above the old high) get a bit more confidence.
        confidence = round(min(0.9, 0.5 + 0.2 * (c.close - level) / a), 2)
        hours = cfg.bar_minutes * cfg.breakout_bars // 60
        span = f"{hours // 24} günün" if hours >= 48 else f"{hours} saatin"
        level_tr = f"{level:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        why = f"{trend_name} trend yukarı ve fiyat son {span} zirvesini ({level_tr}) yukarı kırdı"
        return Signal(
            symbol=sym, quote_currency=sym.quote, direction=Direction.LONG, confidence=confidence,
            expected_entry=c.close, invalidation_price=stop, target_zones=(),
            time_horizon=f"{bar_name(cfg.bar_minutes)} mumlar, birkaç saat - birkaç hafta",
            required_liquidity=None, max_allowed_slippage_bps=None,
            rationale=why, feature_snapshot=features, strategy_version=v, bar_ts=c.ts,
        )

    def generate_signal(
        self, market_state: MarketState, portfolio_state: PortfolioState, risk_state: RiskState
    ) -> Signal:
        sym = market_state.symbol
        if sym in portfolio_state.open_symbols:
            return flat(sym, "bu çiftte zaten açık pozisyon var", self.version)
        if not risk_state.new_positions_allowed:
            return flat(sym, "risk kuralları yeni işleme izin vermiyor", self.version)
        if not market_state.candles:
            return flat(sym, "mum verisi yok", self.version)
        p = self.prepare(sym, market_state.candles)
        return self.signal_at(p, len(p.candles) - 1)
