"""Paper portfolio: balances per currency, open positions, realized PnL in TRY and USD.

Implements the risk engine's PortfolioView. Everything is kept in data/paper_state.json
(written atomically after every change) so a restart keeps balances and positions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptobot.domain.models import Symbol
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService


class PaperStateError(Exception):
    """The saved paper account exists but cannot be read."""


@dataclass
class PaperPosition:
    trade_id: str
    symbol: Symbol
    quantity: Decimal  # base units
    entry_price: Decimal  # average fill, quote currency
    cost: Decimal  # quote paid incl. fee
    cost_try: Decimal
    cost_usd: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    opened_at: datetime
    mark_price: Decimal  # last seen bid, used when no fresh price exists
    # Exit module state (see strategy/exits.py); survives a restart.
    initial_stop: Decimal | None = None
    highest: Decimal | None = None  # best candle close since entry
    breakeven: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": str(self.symbol),
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "cost": str(self.cost),
            "cost_try": str(self.cost_try),
            "cost_usd": str(self.cost_usd),
            "stop_price": str(self.stop_price),
            "target_price": None if self.target_price is None else str(self.target_price),
            "opened_at": self.opened_at.isoformat(),
            "mark_price": str(self.mark_price),
            "initial_stop": None if self.initial_stop is None else str(self.initial_stop),
            "highest": None if self.highest is None else str(self.highest),
            "breakeven": self.breakeven,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PaperPosition:
        return cls(
            trade_id=str(raw["trade_id"]),
            symbol=Symbol.parse(raw["symbol"]),
            quantity=Decimal(raw["quantity"]),
            entry_price=Decimal(raw["entry_price"]),
            cost=Decimal(raw["cost"]),
            cost_try=Decimal(raw["cost_try"]),
            cost_usd=Decimal(raw["cost_usd"]),
            stop_price=Decimal(raw["stop_price"]),
            target_price=None if raw.get("target_price") is None else Decimal(raw["target_price"]),
            opened_at=datetime.fromisoformat(raw["opened_at"]),
            mark_price=Decimal(raw.get("mark_price", raw["entry_price"])),
            initial_stop=None if raw.get("initial_stop") is None else Decimal(raw["initial_stop"]),
            highest=None if raw.get("highest") is None else Decimal(raw["highest"]),
            breakeven=bool(raw.get("breakeven", False)),
        )


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    trade_id: str
    symbol: Symbol
    quantity: Decimal
    exit_price: Decimal
    pnl_try: Decimal
    pnl_usd: Decimal


class PaperPortfolio:
    def __init__(
        self,
        capital: Decimal,
        capital_currency: str,
        risk_currency: str,
        md: MarketDataService,
        fx: FxRateService,
        state_path: Path | None,
    ) -> None:
        self._md = md
        self._fx = fx
        self._risk_ccy = risk_currency
        self._path = state_path
        self.starting_capital = capital
        self.starting_currency = capital_currency
        self.balances: dict[str, Decimal] = {capital_currency: capital}
        self.positions: dict[Symbol, PaperPosition] = {}
        self.realized_try = Decimal(0)
        self.realized_usd = Decimal(0)
        self.fees_try = Decimal(0)
        self.trades_closed = 0
        self.next_id = 1
        self.restored = False
        self.paper = True  # False when this ledger mirrors a real exchange account
        if state_path is not None and state_path.exists():
            self._load(state_path)
            self.restored = True

    # ------------------------------------------------------------ valuation

    def to_risk(self, amount: Decimal, currency: str) -> Decimal | None:
        """Convert to the risk currency (TRY). None if the FX rate is missing."""
        if currency == self._risk_ccy:
            return amount
        usd = self._fx.to_usd(amount, currency)
        rate = self._fx.usd_try()
        if usd is None or rate is None:
            return None
        return usd * rate.rate

    def to_usd(self, amount: Decimal, currency: str) -> Decimal | None:
        return self._fx.to_usd(amount, currency)

    def mark(self, pos: PaperPosition) -> Decimal:
        """Current sell price (best bid) of a position, or the last one seen."""
        t = self._md.ticker(pos.symbol)
        if t is not None and t.bid > 0 and not self._md.is_stale(pos.symbol):
            pos.mark_price = t.bid
        return pos.mark_price

    def position_value(self, pos: PaperPosition) -> Decimal:
        """Value in TRY at the current bid (fees for selling not deducted)."""
        price = self.mark(pos)
        value = self.to_risk(pos.quantity * price, pos.symbol.quote)
        if value is None:  # no FX rate right now: scale the entry value
            value = pos.cost_try * price / pos.entry_price
        return value

    def unrealized_try(self, pos: PaperPosition) -> Decimal:
        return self.position_value(pos) - pos.cost_try

    def _stop_value(self, pos: PaperPosition, worse_bps: Decimal = Decimal(0)) -> Decimal:
        """TRY value if the position were sold at its stop, `worse_bps` below it."""
        amount = pos.quantity * pos.stop_price * (1 - worse_bps / 10_000)
        value = self.to_risk(amount, pos.symbol.quote)
        return value if value is not None else pos.cost_try * amount / (pos.quantity * pos.entry_price)

    def open_risk(self, worse_bps: Decimal) -> Decimal:
        """How much equity would fall if every open position stopped out now, `worse_bps` past its stop."""
        return sum((max(Decimal(0), self.position_value(p) - self._stop_value(p, worse_bps))
                    for p in self.positions.values()), Decimal(0))

    def booked_equity(self) -> Decimal:
        """Cash plus each position at what it cost or at its stop, whichever is higher:
        profit already taken or locked in by a raised stop, without the paper profit on top."""
        return self.cash_try() + sum((max(p.cost_try, self._stop_value(p)) for p in self.positions.values()),
                                     Decimal(0))

    def cash_try(self) -> Decimal:
        total = Decimal(0)
        for ccy, amount in self.balances.items():
            value = self.to_risk(amount, ccy) if amount else None
            if value is not None:
                total += value
        return total

    # PortfolioView ---------------------------------------------------------

    def equity(self) -> Decimal:
        return self.cash_try() + sum((self.position_value(p) for p in self.positions.values()), Decimal(0))

    def open_symbols(self) -> set[Symbol]:
        return set(self.positions)

    # ------------------------------------------------------------ bookkeeping

    def new_trade_id(self) -> str:
        trade_id = f"P{self.next_id:05d}"
        self.next_id += 1
        return trade_id

    def open_position(
        self, trade_id: str, symbol: Symbol, quantity: Decimal, price: Decimal, fee: Decimal,
        stop: Decimal, target: Decimal | None, now: datetime,
    ) -> PaperPosition:
        cost = quantity * price + fee
        cash = self.balances.get(symbol.quote, Decimal(0))
        if cost > cash:
            raise ValueError(f"not enough {symbol.quote}: need {cost}, have {cash}")
        cost_try = self.to_risk(cost, symbol.quote)
        cost_usd = self.to_usd(cost, symbol.quote)
        if cost_try is None or cost_usd is None:
            raise ValueError("FX rate missing, cannot value the trade")
        self.balances[symbol.quote] = cash - cost
        self.fees_try += self.to_risk(fee, symbol.quote) or Decimal(0)
        pos = PaperPosition(trade_id, symbol, quantity, price, cost, cost_try, cost_usd,
                            stop, target, now, price, initial_stop=stop, highest=price)
        self.positions[symbol] = pos
        self.save()
        return pos

    def close_position(self, symbol: Symbol, price: Decimal, fee: Decimal) -> ClosedTrade:
        pos = self.positions.pop(symbol)
        proceeds = pos.quantity * price - fee
        self.balances[symbol.quote] = self.balances.get(symbol.quote, Decimal(0)) + proceeds
        proceeds_try = self.to_risk(proceeds, symbol.quote)
        proceeds_usd = self.to_usd(proceeds, symbol.quote)
        # Without a rate, value the exit with the entry-time rate.
        if proceeds_try is None:
            proceeds_try = pos.cost_try * proceeds / pos.cost
        if proceeds_usd is None:
            proceeds_usd = pos.cost_usd * proceeds / pos.cost
        pnl_try = proceeds_try - pos.cost_try
        # USD result includes the USD/TRY move between entry and exit (FX risk).
        pnl_usd = proceeds_usd - pos.cost_usd
        self.realized_try += pnl_try
        self.realized_usd += pnl_usd
        self.fees_try += self.to_risk(fee, symbol.quote) or Decimal(0)
        self.trades_closed += 1
        self.save()
        return ClosedTrade(pos.trade_id, symbol, pos.quantity, price, pnl_try, pnl_usd)

    def convert_cash(self, target: str, fee_bps: Decimal) -> list[tuple[str, Decimal, Decimal]]:
        """Change every other cash balance into `target` at the live rate, minus the fee.

        Returns (currency, amount sold, target amount received) per conversion.
        A balance without a fresh rate waits for the next call.
        """
        done = []
        unit_try = self.to_risk(Decimal(1), target)
        for ccy, amount in list(self.balances.items()):
            if ccy == target or amount <= 0 or not unit_try:
                continue
            value_try = self.to_risk(amount, ccy)
            if value_try is None:
                continue
            fee_try = value_try * fee_bps / 10_000
            got = (value_try - fee_try) / unit_try
            del self.balances[ccy]
            self.balances[target] = self.balances.get(target, Decimal(0)) + got
            self.fees_try += fee_try
            done.append((ccy, amount, got))
        if done:
            self.save()
        return done

    # ------------------------------------------------------------ views

    def snapshot(self) -> dict[str, Any]:
        equity = self.equity()
        rate = self._fx.usd_try()
        positions = []
        for pos in self.positions.values():
            value = self.position_value(pos)
            pnl = value - pos.cost_try
            positions.append({
                "trade_id": pos.trade_id,
                "pair": str(pos.symbol),
                "quantity": f"{pos.quantity.normalize():f}",
                "entry_price": f"{pos.entry_price:.2f}",
                "price": f"{pos.mark_price:.2f}",
                "stop_price": f"{pos.stop_price:.2f}",
                "target_price": None if pos.target_price is None else f"{pos.target_price:.2f}",
                "value_try": f"{value:.2f}",
                "unrealized_try": f"{pnl:.2f}",
                "unrealized_pct": f"{(pnl / pos.cost_try * 100) if pos.cost_try else 0:.2f}",
                "opened_at": pos.opened_at.isoformat(timespec="seconds"),
            })
        return {
            "paper": self.paper,
            "equity_try": f"{equity:.2f}",
            "equity_usd": None if rate is None else f"{equity / rate.rate:.2f}",
            "cash_try": f"{self.cash_try():.2f}",
            "balances": {k: f"{v:.8f}".rstrip("0").rstrip(".") for k, v in self.balances.items()},
            "starting_capital": f"{self.starting_capital:.2f}",
            "total_change_try": f"{equity - self.starting_capital_try():.2f}",
            "realized_try": f"{self.realized_try:.2f}",
            "realized_usd": f"{self.realized_usd:.2f}",
            "fees_try": f"{self.fees_try:.2f}",
            "trades_closed": self.trades_closed,
            "positions": positions,
        }

    def starting_capital_try(self) -> Decimal:
        return self.to_risk(self.starting_capital, self.starting_currency) or self.starting_capital

    # ------------------------------------------------------------ persistence

    def save(self) -> None:
        if self._path is None:
            return
        data = {
            "starting_capital": str(self.starting_capital),
            "starting_currency": self.starting_currency,
            "balances": {k: str(v) for k, v in self.balances.items()},
            "positions": [p.to_json() for p in self.positions.values()],
            "realized_try": str(self.realized_try),
            "realized_usd": str(self.realized_usd),
            "fees_try": str(self.fees_try),
            "trades_closed": self.trades_closed,
            "next_id": self.next_id,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.starting_capital = Decimal(raw["starting_capital"])
            self.starting_currency = str(raw["starting_currency"])
            self.balances = {k: Decimal(v) for k, v in raw["balances"].items()}
            self.positions = {}
            for item in raw.get("positions", []):
                pos = PaperPosition.from_json(item)
                self.positions[pos.symbol] = pos
            self.realized_try = Decimal(raw.get("realized_try", "0"))
            self.realized_usd = Decimal(raw.get("realized_usd", "0"))
            self.fees_try = Decimal(raw.get("fees_try", "0"))
            self.trades_closed = int(raw.get("trades_closed", 0))
            self.next_id = int(raw.get("next_id", 1))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            raise PaperStateError(f"{path}: {exc}") from exc
