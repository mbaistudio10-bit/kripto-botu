"""Live strategy runner: on every newly closed candle, apply the exit rules to open
positions and ask the strategy for entries. Entries go through PaperTrader.open,
which sizes them with the RiskEngine and lets it refuse them."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from cryptobot.config import ExitConfig, StrategyConfig
from cryptobot.domain.models import Symbol
from cryptobot.events import EventLog, Level
from cryptobot.fmt import fmt_num
from cryptobot.market_data.candles import CandleService
from cryptobot.paper.portfolio import PaperPosition
from cryptobot.paper.trader import PaperTrader
from cryptobot.risk.engine import HaltLevel, RiskEngine
from cryptobot.strategy.base import MarketState, PortfolioState, RiskState, Signal
from cryptobot.strategy.exits import ExitState, on_bar_close
from cryptobot.strategy.trend import Prepared, TrendBreakoutStrategy

log = logging.getLogger(__name__)


class StrategyRunner:
    def __init__(
        self, trader: PaperTrader, engine: RiskEngine, candles: CandleService,
        strategy: TrendBreakoutStrategy, config: StrategyConfig, exits: ExitConfig,
        events: EventLog, pairs: list[Symbol],
    ) -> None:
        self._trader = trader
        self._pf = trader.portfolio
        self._engine = engine
        self._candles = candles
        self._strategy = strategy
        self._cfg = config
        self._exits = exits
        self._events = events
        self.pairs = [p for p in config.pairs if p in pairs]
        self._seen: dict[Symbol, datetime] = {}
        self.last_signal: dict[Symbol, Signal] = {}

    async def run(self, every_seconds: float = 5) -> None:
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001 - logged as a system error, loop goes on
                log.exception("strategy step failed")
            await asyncio.sleep(every_seconds)

    def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._engine.positions_must_close:
            return  # the paper trader is closing everything
        for symbol in self.pairs:
            bars = self._candles.candles(symbol)
            if len(bars) < self._strategy.warmup_bars or self._seen.get(symbol) == bars[-1].ts:
                continue
            self._seen[symbol] = bars[-1].ts
            if not self._on_new_bar(symbol, now):
                del self._seen[symbol]  # e.g. no fresh price to sell at: try again shortly

    def _on_new_bar(self, symbol: Symbol, now: datetime) -> bool:
        bars = self._candles.candles(symbol)
        bar_close = bars[-1].ts + self._strategy.bar
        pos = self._pf.positions.get(symbol)
        if pos is not None:
            prep = self._strategy.prepare(symbol, bars)
            return self._manage(pos, prep, bar_close, now)
        if now - bar_close > timedelta(minutes=self._cfg.act_within_minutes):
            return True  # old candle (e.g. right after start): never chase a stale signal
        halts = self._engine.halts()
        sig = self._strategy.generate_signal(
            MarketState(symbol, bars, now),
            PortfolioState(frozenset(self._pf.open_symbols())),
            RiskState(self._engine.halt_level is HaltLevel.NORMAL, [h.message for h in halts]),
        )
        self.last_signal[symbol] = sig
        if not sig.is_entry:
            return True
        assert sig.expected_entry is not None and sig.invalidation_price is not None
        stop_pct = Decimal(str(round((sig.expected_entry - sig.invalidation_price)
                                     / sig.expected_entry * 100, 4)))
        self._events.emit(Level.BILGI, "signal", f"{symbol}: alış sinyali. {sig.rationale}",
                          symbol=str(symbol), confidence=sig.confidence, stop_pct=stop_pct,
                          features=sig.feature_snapshot, strategy=sig.strategy_version)
        reasons = self._trader.open(symbol, stop_pct, None, now,
                                    note=f"{sig.rationale} (güven {sig.confidence:.0%})")
        if reasons:
            log.info("signal for %s refused: %s", symbol, "; ".join(reasons))
            halt_msgs = {h.message for h in halts}
            problems = [r for r in reasons if r not in halt_msgs]
            if problems:
                # Rate limited by the notifier (same key at most every 30 minutes).
                self._events.emit(
                    Level.BILGI, "entry_blocked",
                    f"{symbol}: strateji alış sinyali verdi ama risk kuralları izin vermedi:\n• "
                    + "\n• ".join(problems),
                    notify=True, key=str(symbol), problems=problems,
                )
        return True

    def _manage(self, pos: PaperPosition, prep: Prepared, bar_close: datetime, now: datetime) -> bool:
        if bar_close <= pos.opened_at:
            return True  # candle finished before we bought
        i = len(prep.candles) - 1
        st = ExitState(
            entry=float(pos.entry_price),
            initial_stop=float(pos.initial_stop if pos.initial_stop is not None else pos.stop_price),
            stop=float(pos.stop_price),
            highest=float(pos.highest if pos.highest is not None else pos.entry_price),
            bars_held=int((bar_close - pos.opened_at) / self._strategy.bar) + 1,
            breakeven=pos.breakeven,
        )
        decision = on_bar_close(self._exits, st, prep.candles[i].close, prep.atr[i],
                                self._strategy.trend_ok(prep, i))
        if decision.exit_reason is not None:
            return self._trader.close(pos.symbol, decision.exit_reason, decision.note, now)
        pos.highest = Decimal(str(st.highest))
        pos.breakeven = st.breakeven
        if decision.stop_moved:
            pos.stop_price = Decimal(str(st.stop))
            if decision.moved_to_breakeven:
                self._events.emit(
                    Level.BILGI, "stop_breakeven",
                    f"🔒 {pos.symbol}: stop alış fiyatının üstüne çekildi ({fmt_num(pos.stop_price)}). "
                    "Fiyat aniden çok sert düşmezse bu işlem artık zararla kapanmaz.",
                    notify=True, key=pos.trade_id, symbol=str(pos.symbol), stop_price=pos.stop_price,
                )
            else:
                self._events.emit(Level.BILGI, "stop_trailed",
                                  f"{pos.symbol}: iz süren stop yükseldi ({fmt_num(pos.stop_price)}).",
                                  symbol=str(pos.symbol), stop_price=pos.stop_price)
        self._pf.save()
        return True

    def snapshot(self) -> dict[str, Any]:
        pairs = {}
        for symbol in self.pairs:
            bars = self._candles.candles(symbol)
            sig = self.last_signal.get(symbol)
            pairs[str(symbol)] = {
                "ready": len(bars) >= self._strategy.warmup_bars,
                "last_candle": bars[-1].ts.isoformat() if bars else None,
                "decision": sig.direction.value if sig else None,
                "why": sig.rationale if sig else None,
            }
        return {"name": self._strategy.name, "version": self._strategy.version,
                "bar_minutes": self._cfg.bar_minutes, "trend_bar_minutes": self._cfg.trend_bar_minutes,
                "pairs": pairs}

