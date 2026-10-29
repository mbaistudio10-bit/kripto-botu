from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cryptobot.config import Settings
from cryptobot.domain.models import BookLevel, OrderBook, Symbol
from cryptobot.events import STOP_LOSS_HIT, TRADE_CLOSED, TRADE_OPENED, EventLog
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService
from cryptobot.paper.portfolio import PaperPortfolio, PaperStateError
from cryptobot.paper.trader import PaperTrader, walk_book
from cryptobot.report import build_report
from cryptobot.risk.engine import RiskEngine
from tests.conftest import FakeAdapter, make_ticker

BTC, ETH, USDT = Symbol("BTC", "TRY"), Symbol("ETH", "TRY"), Symbol("USDT", "TRY")


def set_price(md: MarketDataService, sym: Symbol, price: str, now: datetime, depth_try: str = "1000000") -> None:
    p = Decimal(price)
    md.on_ticker(replace(make_ticker(str(sym), price), received_ts=now, volume_24h_quote=Decimal(10**9)))
    size = Decimal(depth_try) / p
    md._books[sym] = OrderBook(sym, (BookLevel(p, size),), (BookLevel(p, size),),
                               exchange_ts=now, received_ts=now)


class Rig:
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        self.now = datetime.now(timezone.utc)
        self.settings = settings
        self.tmp = tmp_path
        self.md = MarketDataService(FakeAdapter({}), [BTC, ETH, USDT], settings.market_data)
        self.fx = FxRateService(self.md, settings.fx)
        for sym, price in ((BTC, "3500000"), (ETH, "130000"), (USDT, "35")):
            set_price(self.md, sym, price, self.now)
        self.events = EventLog(tmp_path / "events.jsonl")
        self.seen = []
        self.events.subscribe(self.seen.append)
        self.portfolio = self.new_portfolio()
        self.engine = RiskEngine(settings, self.md, self.fx, self.portfolio, self.events,
                                 tmp_path / "risk_state.json", started_at=self.now - timedelta(hours=1))
        self.engine.evaluate(self.now)
        # These tests trade TL pairs, so idle money stays in TL.
        self.paper_cfg = settings.paper.model_copy(update={"cash_currency": "TRY"})
        self.trader = PaperTrader(self.portfolio, self.engine, self.md, self.events, self.paper_cfg)

    def buy(self, sym: Symbol = BTC, note: str | None = None) -> list[str]:
        return self.trader.open(sym, Decimal(1), Decimal("1.5"), self.now, note=note)

    def new_portfolio(self) -> PaperPortfolio:
        return PaperPortfolio(Decimal(10000), "TRY", "TRY", self.md, self.fx, self.tmp / "paper_state.json")

    def kinds(self) -> list[str]:
        return [e.kind for e in self.seen]


@pytest.fixture
def rig(settings: Settings, tmp_path: Path) -> Rig:
    return Rig(settings, tmp_path)


def test_walk_book_averages_levels_and_penalises_thin_books() -> None:
    now = datetime.now(timezone.utc)
    book = OrderBook(BTC, (BookLevel(Decimal(99), Decimal(1)),),
                     (BookLevel(Decimal(100), Decimal(1)), BookLevel(Decimal(102), Decimal(1))), now)
    assert walk_book(book, "buy", Decimal(2)) == Decimal(101)
    # 1 unit at 99, the missing unit at 99 * (1 - 0.5%)
    assert walk_book(book, "sell", Decimal(2)) == (Decimal(99) + Decimal("98.505")) / 2


def test_open_risk_sized_position_and_notifies(rig: Rig) -> None:
    assert rig.buy(note="trend yukarı") == []
    assert rig.portfolio.open_symbols() == {BTC}
    pos = rig.portfolio.positions[BTC]
    # Loss at the stop (1% + 0.2% costs) stays within 1% of the account.
    risk = pos.quantity * (pos.entry_price - pos.stop_price) + pos.cost * Decimal("0.002")
    assert risk <= Decimal(100)
    assert pos.cost <= Decimal(10000) * Decimal("0.45") * Decimal("1.001")
    assert pos.target_price == pos.entry_price * Decimal("1.015")
    assert rig.portfolio.balances["TRY"] == Decimal(10000) - pos.cost
    opened = [e for e in rig.seen if e.kind == TRADE_OPENED]
    assert len(opened) == 1 and opened[0].should_notify and "(deneme)" in opened[0].message
    assert "Sebep: trend yukarı" in opened[0].message
    # Equity drops only by the entry fee while the price is unchanged.
    assert Decimal(10000) - rig.portfolio.equity() == pytest.approx(pos.cost - pos.quantity * pos.entry_price)


def test_stop_loss_closes_with_loss_in_try_and_usd(rig: Rig) -> None:
    rig.buy()
    set_price(rig.md, BTC, "3460000", rig.now)  # below the 1% stop
    rig.trader.step(rig.now)
    assert not rig.portfolio.positions
    hit = [e for e in rig.seen if e.kind == STOP_LOSS_HIT]
    assert len(hit) == 1
    assert hit[0].data["pnl_try"] < 0 and hit[0].data["pnl_usd"] < 0
    assert rig.portfolio.realized_try == hit[0].data["pnl_try"]
    assert rig.portfolio.equity() == pytest.approx(Decimal(10000) + rig.portfolio.realized_try)
    report = build_report(rig.events.read_since(rig.now - timedelta(minutes=1)), "gün", rig.now - timedelta(minutes=1))
    assert report.trades == 1 and report.losses == 1


def test_target_exit_is_profit_and_usd_includes_fx_move(rig: Rig) -> None:
    rig.buy()
    set_price(rig.md, BTC, "3600000", rig.now)
    set_price(rig.md, USDT, "36", rig.now)  # lira weakened 2.9% meanwhile
    rig.trader.step(rig.now)
    closed = [e for e in rig.seen if e.kind == TRADE_CLOSED]
    assert len(closed) == 1 and "hedef" in closed[0].message
    assert closed[0].data["pnl_try"] > 0
    # +2.9% in TRY but the lira lost 2.8% against the dollar: much smaller in USD.
    assert closed[0].data["pnl_usd"] < closed[0].data["pnl_try"] / 35


def test_no_exit_on_stale_price(rig: Rig) -> None:
    rig.buy()
    old = rig.now - timedelta(minutes=5)
    set_price(rig.md, BTC, "3000000", old)  # crash, but the price is old
    rig.trader.step(rig.now)
    assert BTC in rig.portfolio.positions


def test_manual_stop_blocks_entries_but_keeps_managing_positions(rig: Rig) -> None:
    rig.buy()
    rig.engine.stop("test")
    later = rig.now + timedelta(minutes=31)
    for sym, price in ((BTC, "3400000"), (ETH, "130000"), (USDT, "35")):
        set_price(rig.md, sym, price, later)
    assert any("Elle durduruldu" in r for r in rig.trader.open(ETH, Decimal(1), None, later))
    rig.trader.step(later)
    assert ETH not in rig.portfolio.positions  # no new trade while stopped
    assert BTC not in rig.portfolio.positions  # but the stop-loss still worked


def test_drawdown_halt_closes_everything(rig: Rig) -> None:
    rig.buy()
    rig.engine._state.drawdown_halt = True
    rig.trader.step(rig.now)
    assert not rig.portfolio.positions
    assert "acil kapatma" in [e.data.get("reason") for e in rig.seen if e.kind == TRADE_CLOSED]


def test_thin_pair_is_refused(rig: Rig) -> None:
    set_price(rig.md, BTC, "3500000", rig.now, depth_try="1000")
    reasons = rig.buy()
    assert not rig.portfolio.positions
    assert any("sığ" in r for r in reasons)


def test_raised_stop_hit_is_a_profit_sale_not_a_stop_loss(rig: Rig) -> None:
    rig.buy()
    pos = rig.portfolio.positions[BTC]
    pos.stop_price = pos.entry_price * Decimal("1.01")
    set_price(rig.md, BTC, str(pos.entry_price * Decimal("1.005")), rig.now)
    rig.trader.step(rig.now)
    assert BTC not in rig.portfolio.positions
    assert STOP_LOSS_HIT not in rig.kinds()
    assert "kâr korundu" in [e for e in rig.seen if e.kind == TRADE_CLOSED][0].message


def test_state_survives_restart(rig: Rig) -> None:
    rig.buy()
    again = rig.new_portfolio()
    assert again.restored and again.open_symbols() == {BTC}
    assert again.balances == rig.portfolio.balances
    assert again.positions[BTC].stop_price == rig.portfolio.positions[BTC].stop_price


def test_corrupt_state_raises(rig: Rig) -> None:
    (rig.tmp / "paper_state.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PaperStateError):
        rig.new_portfolio()


def test_snapshot_has_try_and_usd(rig: Rig) -> None:
    rig.buy()
    snap = rig.portfolio.snapshot()
    assert Decimal(snap["equity_usd"]) == pytest.approx(Decimal(snap["equity_try"]) / 35, rel=Decimal("1e-3"))
    assert snap["positions"][0]["pair"] == "BTC/TRY"


def test_idle_tl_is_changed_into_dollars_once(rig: Rig) -> None:
    trader = PaperTrader(rig.portfolio, rig.engine, rig.md, rig.events, rig.settings.paper)
    trader.step(rig.now)
    pf = rig.portfolio
    assert set(pf.balances) == {"USDT"}
    # 10,000 TL at 35 TL per USDT, minus the 0.10% fee.
    assert pf.balances["USDT"] == pytest.approx(Decimal(10000) / 35 * Decimal("0.999"))
    assert pf.fees_try == pytest.approx(Decimal(10))
    assert pf.equity() == pytest.approx(Decimal(9990))
    assert rig.kinds().count("cash_converted") == 1
    trader.step(rig.now)
    assert rig.kinds().count("cash_converted") == 1
    assert "USDT" in rig.new_portfolio().balances  # saved


def test_idle_tl_waits_for_a_dollar_rate(rig: Rig) -> None:
    rig.md._tickers.pop(USDT)
    trader = PaperTrader(rig.portfolio, rig.engine, rig.md, rig.events, rig.settings.paper)
    trader.step(rig.now)
    assert rig.portfolio.balances == {"TRY": Decimal(10000)}


def test_position_on_a_pair_no_longer_traded_is_sold(rig: Rig) -> None:
    assert rig.buy() == []
    trader = PaperTrader(rig.portfolio, rig.engine, rig.md, rig.events, rig.paper_cfg, {ETH})
    trader.step(rig.now)
    assert not rig.portfolio.positions
    closed = [e for e in rig.seen if e.kind == TRADE_CLOSED]
    assert len(closed) == 1 and "ayar değişti" in closed[0].message


def test_dollar_pair_is_bought_with_dollars(rig: Rig) -> None:
    btc_usdt = Symbol("BTC", "USDT")
    md = MarketDataService(FakeAdapter({}), [btc_usdt, USDT], rig.settings.market_data)
    fx = FxRateService(md, rig.settings.fx)
    set_price(md, USDT, "35", rig.now)
    set_price(md, btc_usdt, "100000", rig.now, depth_try="30000")  # 30,000 USDT ≈ 1M TL each side
    pf = PaperPortfolio(Decimal(10000), "TRY", "TRY", md, fx, None)
    engine = RiskEngine(rig.settings, md, fx, pf, rig.events, None,
                        started_at=rig.now - timedelta(hours=1))
    engine.evaluate(rig.now)
    trader = PaperTrader(pf, engine, md, rig.events, rig.settings.paper, {btc_usdt})
    trader.step(rig.now)
    assert trader.open(btc_usdt, Decimal(1), None, rig.now) == []
    pos = pf.positions[btc_usdt]
    assert pos.cost_try == pytest.approx(pos.cost * 35)
    assert pf.balances["USDT"] == pytest.approx(Decimal(10000) / 35 * Decimal("0.999") - pos.cost)
    opened = [e for e in rig.seen if e.kind == TRADE_OPENED][-1]
    assert "USDT\n" in opened.message and "TL" in opened.message


def test_two_open_trades_stopping_out_together_stay_above_the_floor(rig: Rig) -> None:
    # Push the account near the floor so the capital guard, not the 1% rule, sizes the trades.
    rig.portfolio.balances["TRY"] = Decimal("9880")
    rig.engine.evaluate(rig.now)
    floor = rig.engine.capital_floor()
    assert floor == Decimal("9800")
    assert rig.trader.open(BTC, Decimal(3), None, rig.now) == []
    assert rig.trader.open(ETH, Decimal(3), None, rig.now) == []
    # The second trade must count the risk the first one already carries.
    worst = rig.portfolio.equity() - rig.portfolio.open_risk(Decimal(70))
    assert worst >= floor * Decimal("0.9999")
    # Both stop out at once, 0.5% past their stops: the account still ends above the floor.
    for sym in (BTC, ETH):
        pos = rig.portfolio.positions[sym]
        set_price(rig.md, sym, str(pos.stop_price * Decimal("0.995")), rig.now)
    rig.trader.step(rig.now)
    assert not rig.portfolio.positions
    assert rig.portfolio.equity() >= floor * Decimal("0.999")


def test_paper_profit_does_not_raise_the_floor_until_it_is_locked(rig: Rig) -> None:
    rig.buy()
    pos = rig.portfolio.positions[BTC]
    set_price(rig.md, BTC, "3850000", rig.now)  # +10% on paper
    rig.engine.evaluate(rig.now)
    assert rig.engine.capital_floor() == Decimal("9800")  # a pullback must not stop the bot
    pos.stop_price = pos.entry_price * Decimal("1.05")  # trailing stop locks +5%
    rig.engine.evaluate(rig.now)
    locked = rig.portfolio.booked_equity() - Decimal(10000)
    assert locked > 0
    assert rig.engine.capital_floor() == pytest.approx(Decimal("9800") + locked / 2)


def test_emergency_close_names_the_capital_floor(rig: Rig) -> None:
    rig.buy()
    rig.engine._state.floor_halt = True
    rig.trader.step(rig.now)
    closed = [e for e in rig.seen if e.kind == TRADE_CLOSED][0]
    assert "Ana para koruması" in closed.message
