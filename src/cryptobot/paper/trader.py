"""Paper trader: simulated fills, stop-loss / target exits and emergency close.

Entries come from the strategy runner (strategy/runner.py), which also applies the
bar-close exit rules. Every entry goes through RiskEngine.max_quantity and
RiskEngine.check_trade.
Every fill is announced through emit_trade_opened / emit_trade_closed, which
Telegram (Achilles) and the Jarvis webhook pick up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from cryptobot.config import PaperConfig
from cryptobot.domain.models import OrderBook, Symbol
from cryptobot.events import EventLog, Level, emit_trade_closed, emit_trade_opened
from cryptobot.fmt import fmt_num
from cryptobot.market_data.service import MarketDataService
from cryptobot.paper.portfolio import PaperPortfolio
from cryptobot.risk.engine import RiskEngine, TradeIntent

log = logging.getLogger(__name__)

QTY_STEP = Decimal("0.000001")
# If the visible book is too thin for the whole size, the rest fills this much worse.
THIN_BOOK_PENALTY_BPS = Decimal(50)
_BPS = Decimal(10_000)


def walk_book(book: OrderBook | None, side: str, quantity: Decimal) -> Decimal | None:
    """Average price for taking `quantity` from the book (side 'buy' eats asks)."""
    if book is None:
        return None
    levels = book.asks if side == "buy" else book.bids
    if not levels:
        return None
    left, spent = quantity, Decimal(0)
    for lv in levels:
        take = min(left, lv.size)
        spent += take * lv.price
        left -= take
        if left <= 0:
            break
    if left > 0:
        worst = levels[-1].price
        adj = THIN_BOOK_PENALTY_BPS / _BPS
        spent += left * (worst * (1 + adj) if side == "buy" else worst * (1 - adj))
    return spent / quantity


@dataclass(frozen=True, slots=True)
class Fill:
    price: Decimal
    fee: Decimal


class PaperTrader:
    def __init__(
        self,
        portfolio: PaperPortfolio,
        engine: RiskEngine,
        md: MarketDataService,
        events: EventLog,
        config: PaperConfig,
        traded_pairs: set[Symbol] | None = None,
    ) -> None:
        # Positions outside these pairs (left over after a settings change) are sold.
        self._traded = traded_pairs
        self._pf = portfolio
        self._engine = engine
        self._md = md
        self._events = events
        self._config = config

    @property
    def portfolio(self) -> PaperPortfolio:
        return self._pf

    # ---------------------------------------------------------------- fills

    def _fill(self, symbol: Symbol, side: str, quantity: Decimal, now: datetime) -> Fill | None:
        """Simulated taker fill against the latest book, never better than the live ticker."""
        t = self._md.ticker(symbol)
        if t is None or self._md.is_stale(symbol, now):
            return None
        book = self._md.book(symbol)
        price = walk_book(book, side, quantity)
        if side == "buy":
            price = t.ask if price is None else max(price, t.ask)
        else:
            price = t.bid if price is None else min(price, t.bid)
        if price <= 0:
            return None
        fee = quantity * price * self._config.fee_bps / _BPS
        return Fill(price, fee)

    # ---------------------------------------------------------------- loop

    async def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001 - logged as a system error, loop goes on
                log.exception("paper trader step failed")
            await asyncio.sleep(self._config.check_every_seconds)

    def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._engine.positions_must_close:
            for symbol in list(self._pf.positions):
                self.close(symbol, "acil kapatma", "acil durdurma (zirveden düşüş limiti)", now)
            return
        self._manage_exits(now)
        self._convert_idle_cash()

    def _convert_idle_cash(self) -> None:
        target = self._config.cash_currency
        for ccy, amount, got in self._pf.convert_cash(target, self._config.fee_bps):
            self._events.emit(
                Level.BILGI, "cash_converted",
                f"💱 Deneme hesabı: {fmt_num(amount)} {ccy} → {fmt_num(got)} {target} "
                f"(boştaki para {target} olarak tutulur)",
                currency=ccy, amount=amount, target=target, received=got,
            )

    def _manage_exits(self, now: datetime) -> None:
        for symbol, pos in list(self._pf.positions.items()):
            t = self._md.ticker(symbol)
            if t is None or self._md.is_stale(symbol, now) or t.bid <= 0:
                continue  # no reliable price: never exit on old data
            pos.mark_price = t.bid
            if self._traded is not None and symbol not in self._traded:
                self.close(symbol, "satış", "bu çift artık işlem listesinde değil (ayar değişti)", now)
            elif t.bid <= pos.stop_price:
                if pos.stop_price >= pos.entry_price:  # stop was raised: profit protected
                    self.close(symbol, "satış", "iz süren stop çalıştı (kâr korundu)", now)
                else:
                    self.close(symbol, "stop-loss", None, now)
            elif pos.target_price is not None and t.bid >= pos.target_price:
                self.close(symbol, "satış", "hedef fiyata ulaştı", now)

    # ---------------------------------------------------------------- actions

    def open(
        self, symbol: Symbol, stop_pct: Decimal, target_pct: Decimal | None, now: datetime,
        note: str | None = None,
    ) -> list[str]:
        """Try to open a paper LONG. Returns the reasons it was refused (empty = opened)."""
        t = self._md.ticker(symbol)
        if t is None or self._md.is_stale(symbol, now) or t.ask <= 0:
            return [f"{symbol}: fiyat verisi yok veya eski"]
        cash = self._pf.balances.get(symbol.quote, Decimal(0))
        equity = self._pf.equity()
        est_stop = t.ask * (1 - stop_pct / 100)
        qty = self._engine.max_quantity(symbol, t.ask, est_stop)
        price_try = self._pf.to_risk(t.ask, symbol.quote)
        if qty is None or not price_try:
            return [f"{symbol}: işlem büyüklüğü hesaplanamadı (kur eksik olabilir)"]
        fee_mult = 1 + self._config.fee_bps / _BPS
        cap = equity * self._config.max_position_pct / 100 / price_try
        # Small buffer so slippage while walking the book never overdraws cash.
        cash_cap = cash * Decimal("0.98") / (t.ask * fee_mult)
        qty = min(qty, cap, cash_cap).quantize(QTY_STEP, rounding=ROUND_DOWN)
        if qty <= 0:
            return [f"{symbol}: yeterli nakit yok"]
        fill = self._fill(symbol, "buy", qty, now)
        if fill is None:
            return [f"{symbol}: fiyat verisi yok veya eski"]
        stop = fill.price * (1 - stop_pct / 100)
        target = None if target_pct is None else fill.price * (1 + target_pct / 100)
        decision = self._engine.check_trade(TradeIntent(symbol, qty, fill.price, stop), now)
        if not decision.allowed:
            return decision.reasons
        trade_id = self._pf.new_trade_id()
        try:
            self._pf.open_position(trade_id, symbol, qty, fill.price, fill.fee, stop, target, now)
        except ValueError as exc:
            return [f"{symbol}: {exc}"]
        value_try = self._pf.positions[symbol].cost_try
        emit_trade_opened(self._events, trade_id=trade_id, symbol=str(symbol), quantity=qty,
                          price=fill.price, value_try=value_try, stop_price=stop, target_price=target,
                          note=note)
        return []

    def close(self, symbol: Symbol, reason: str, note: str | None, now: datetime) -> bool:
        pos = self._pf.positions.get(symbol)
        if pos is None:
            return False
        fill = self._fill(symbol, "sell", pos.quantity, now)
        if fill is None:
            log.warning("cannot close paper %s now: no fresh price", symbol)
            return False
        done = self._pf.close_position(symbol, fill.price, fill.fee)
        emit_trade_closed(self._events, trade_id=done.trade_id, symbol=str(symbol),
                          quantity=done.quantity, price=done.exit_price, pnl_try=done.pnl_try,
                          pnl_usd=done.pnl_usd, reason=reason, note=note)
        return True
