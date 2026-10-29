"""Live trader: the same entry/exit rules as the paper trader, but with real orders.

How it differs from PaperTrader:
- Sizing and the risk engine's checks are unchanged (every buy still needs its OK).
- A buy is an immediate-or-cancel limit order a few hundredths of a percent above the best
  ask, so a sudden jump cannot fill it at a much worse price. A sell is a market order:
  getting out must never be blocked by a price limit.
- What really happened (fill price, amount, fee) is written into the bot's books, not the estimate.
- The bot's books live in data/live_state.json and are compared with the real account
  regularly; a difference stops the bot (someone sold or moved coins by hand).
- Stops are watched by this program. If the computer is off, there is no stop-loss on the exchange.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptobot.config import LiveConfig, PaperConfig
from cryptobot.domain.models import Instrument, Symbol
from cryptobot.events import EventLog, Level, emit_trade_opened
from cryptobot.exchanges.base import ExchangeError
from cryptobot.exchanges.okx_private import (
    OkxPrivateClient, OrderResult, ceil_to_step, floor_to_step,
)
from cryptobot.fmt import fmt_num
from cryptobot.market_data.service import MarketDataService
from cryptobot.paper.portfolio import PaperPortfolio, PaperPosition
from cryptobot.paper.trader import Fill, PaperTrader
from cryptobot.risk.engine import RiskEngine

log = logging.getLogger(__name__)

SELL_RETRY_SECONDS = 30
_BPS = Decimal(10_000)


class LiveTrader(PaperTrader):
    is_paper = False

    def __init__(
        self,
        portfolio: PaperPortfolio,
        engine: RiskEngine,
        md: MarketDataService,
        events: EventLog,
        config: PaperConfig,
        live: LiveConfig,
        client: OkxPrivateClient,
        instruments: dict[Symbol, Instrument],
        traded_pairs: set[Symbol] | None = None,
    ) -> None:
        super().__init__(portfolio, engine, md, events, config, traded_pairs)
        self._live = live
        self._client = client
        self._instruments = instruments
        self._buys: list[datetime] = []
        self._sell_retry_at: dict[Symbol, datetime] = {}

    # ---------------------------------------------------------------- hooks

    def _quantity_step(self, symbol: Symbol) -> Decimal:
        return self._instruments[symbol].lot_size

    def _check_order_size(self, symbol: Symbol, quantity: Decimal) -> list[str]:
        inst = self._instruments.get(symbol)
        if inst is None:
            return [f"{symbol}: borsada bu çiftin bilgisi yok"]
        if quantity < inst.min_size:
            return [f"{symbol}: hesaplanan miktar borsanın en küçük emrinden ({inst.min_size.normalize():f}) küçük"]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        self._buys = [t for t in self._buys if t > cutoff]
        if len(self._buys) >= self._live.max_buys_per_day:
            return [f"son 24 saatte {self._live.max_buys_per_day} alış yapıldı, günlük alış sınırı doldu"]
        return []

    def _convert_idle_cash(self) -> None:
        return  # real money is never changed from one currency to another by the bot

    def _enter(
        self, symbol: Symbol, qty: Decimal, fill: Fill, stop_pct: Decimal, target_pct: Decimal | None,
        note: str | None, now: datetime,
    ) -> list[str]:
        inst = self._instruments[symbol]
        t = self._md.ticker(symbol)
        if t is None or t.ask <= 0:
            return [f"{symbol}: fiyat verisi yok"]
        limit = ceil_to_step(t.ask * (1 + self._live.buy_slippage_bps / _BPS), inst.tick_size)
        trade_id = self._pf.new_trade_id()
        try:
            bal = self._client.balances().get(symbol.quote)
            need = qty * limit * (1 + self._config.fee_bps / _BPS)
            if bal is None or bal.available < need:
                have = Decimal(0) if bal is None else bal.available
                return [f"{symbol}: borsada yeterli {symbol.quote} yok (var: {fmt_num(have)}, gerekli: {fmt_num(need)})"]
            self._buys.append(now)
            res = self._send_order(inst, symbol, "buy", "ioc", qty, limit, trade_id)
        except ExchangeError as exc:
            log.error("live buy of %s failed: %s", symbol, exc)
            return [f"{symbol}: alış emri başarısız ({exc})"]
        if res.filled <= 0:
            return [f"{symbol}: alış emri dolmadı (fiyat hızlı kaçtı), para harcanmadı"]
        # Fees on OKX spot buys are usually taken in the coin itself: the coins we hold are fewer.
        fee_quote = res.fee if res.fee_ccy == symbol.quote else (
            res.fee * res.avg_price if res.fee_ccy == symbol.base else Decimal(0))
        held = res.filled - (res.fee if res.fee_ccy == symbol.base else Decimal(0))
        stop = res.avg_price * (1 - stop_pct / 100)
        target = None if target_pct is None else res.avg_price * (1 + target_pct / 100)
        try:
            self._pf.open_position(trade_id, symbol, held, res.avg_price, fee_quote, stop, target, now)
        except ValueError as exc:
            # The order is real but the books refuse it: stop everything and tell the owner.
            self._alarm(f"{symbol} alışı borsada gerçekleşti ama kayıtlara işlenemedi ({exc}). Bot durduruldu, "
                        "borsa hesabınızı kontrol edin.", key="book_error")
            self._engine.stop("canlı kayıt hatası")
            return []
        emit_trade_opened(self._events, trade_id=trade_id, symbol=str(symbol), quantity=held,
                          price=res.avg_price, value_try=self._pf.positions[symbol].cost_try,
                          stop_price=stop, target_price=target, paper=False, note=note)
        return []

    def _exit_fill(self, symbol: Symbol, pos: PaperPosition, now: datetime) -> Fill | None:
        retry_at = self._sell_retry_at.get(symbol)
        if retry_at is not None and now < retry_at:
            return None
        inst = self._instruments[symbol]
        try:
            bal = self._client.balances().get(symbol.base)
            real = Decimal(0) if bal is None else bal.available
            qty = floor_to_step(min(pos.quantity, real), inst.lot_size)
            if qty < inst.min_size:
                raise ExchangeError(
                    f"satılacak {symbol.base} bulunamadı (borsada {real.normalize():f}, kayıtta {pos.quantity.normalize():f})")
            res = self._send_order(inst, symbol, "sell", "market", qty, None, pos.trade_id)
        except ExchangeError as exc:
            self._sell_retry_at[symbol] = now + timedelta(seconds=SELL_RETRY_SECONDS)
            log.error("live sell of %s failed: %s", symbol, exc)
            self._alarm(f"{symbol} satış emri başarısız oldu, {SELL_RETRY_SECONDS} saniye sonra tekrar denenecek: {exc}",
                        key=f"sell_{symbol}")
            return None
        if res.filled <= 0:
            self._sell_retry_at[symbol] = now + timedelta(seconds=SELL_RETRY_SECONDS)
            self._alarm(f"{symbol} satış emri dolmadı, tekrar denenecek.", key=f"sell_{symbol}")
            return None
        self._sell_retry_at.pop(symbol, None)
        if res.filled < pos.quantity * Decimal("0.98"):
            self._alarm(f"{symbol}: kayıttaki {pos.quantity.normalize():f} yerine {res.filled.normalize():f} satıldı. "
                        "Kalan küçük miktarı borsa uygulamasından kontrol edin.", key=f"dust_{symbol}")
        pos.quantity = min(pos.quantity, res.filled)  # books follow what really sold
        fee_quote = res.fee if res.fee_ccy == symbol.quote else res.fee * res.avg_price
        return Fill(res.avg_price, fee_quote)

    # ---------------------------------------------------------------- orders

    def _send_order(
        self, inst: Instrument, symbol: Symbol, side: str, ord_type: str, qty: Decimal,
        price: Decimal | None, trade_id: str,
    ) -> OrderResult:
        client_id = f"kb{trade_id}{side[0]}{int(time.time())}"
        try:
            order_id = self._client.place_order(inst.exchange_symbol, side, ord_type, qty, price, client_id)
        except ExchangeError:
            # The request may have reached the exchange even though the reply was lost.
            found = self._client.get_order(inst.exchange_symbol, client_id=client_id)
            if found is None:
                raise
            order_id = found.order_id
        return self._client.wait_order(inst.exchange_symbol, order_id, self._live.order_wait_seconds)

    def _alarm(self, text: str, key: str) -> None:
        self._events.emit(Level.KRITIK, "live_order_problem", f"🚨 Canlı işlem sorunu\n{text}",
                          notify=True, key=key)

    # ---------------------------------------------------------------- books vs. real account

    def reconcile(self) -> list[str]:
        """Compare the bot's books with the real account. Returns problems found (empty = fine)."""
        balances = self._client.balances()
        problems = []
        for symbol, pos in self._pf.positions.items():
            real = balances.get(symbol.base)
            have = Decimal(0) if real is None else real.total
            if have < pos.quantity * Decimal("0.98"):
                problems.append(f"{symbol.base}: kayıtta {pos.quantity.normalize():f}, borsada {have.normalize():f}")
        return problems

    async def reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self._live.reconcile_every_seconds)
            try:
                problems = await asyncio.to_thread(self.reconcile)
            except ExchangeError as exc:
                log.warning("account check failed: %s", exc)
                continue
            if problems:
                self._alarm("Bot kayıtları borsadaki gerçek bakiyeyle uyuşmuyor, bot durduruldu:\n• "
                            + "\n• ".join(problems), key="reconcile")
                self._engine.stop("borsa bakiyesi kayıtlarla uyuşmuyor")
