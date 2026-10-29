from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from cryptobot.config import Settings
from cryptobot.domain.models import BookLevel, OrderBook, Symbol, TradingMode
from cryptobot.events import EventLog, Level
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService
from cryptobot.risk.engine import HaltLevel, RiskEngine, TradeIntent
from tests.conftest import FakeAdapter, make_ticker

BTC, ETH, USDT = Symbol("BTC", "TRY"), Symbol("ETH", "TRY"), Symbol("USDT", "TRY")
PRICES = {BTC: "3500000", ETH: "130000", USDT: "35"}
# 2026-10-26 is a Monday; 12:00 UTC = 15:00 Turkey time.
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class FakePortfolio:
    def __init__(self, equity: str = "10000") -> None:
        self.value = Decimal(equity)
        self.open: set[Symbol] = set()

    def equity(self) -> Decimal:
        return self.value

    def open_symbols(self) -> set[Symbol]:
        return self.open


def feed(md: MarketDataService, now: datetime, depth_try: str = "1000000") -> None:
    """Fresh, liquid prices and books for every pair."""
    for sym, price in PRICES.items():
        t = make_ticker(str(sym), price)
        md.on_ticker(replace(t, received_ts=now, volume_24h_quote=Decimal(10**9)))
        p = Decimal(price)
        size = Decimal(depth_try) / p
        md._books[sym] = OrderBook(
            sym, (BookLevel(p, size),), (BookLevel(p, size),), exchange_ts=now, received_ts=now
        )


class Rig:
    def __init__(self, settings: Settings, tmp_path: Path, equity: str = "10000", guard: bool = False,
                 **kw) -> None:
        # Most tests check one limit at a time; the capital guard has its own tests.
        self.settings = settings if guard else settings.model_copy(
            update={"capital_guard": settings.capital_guard.model_copy(update={"enabled": False})})
        self.md = MarketDataService(FakeAdapter({}), [BTC, ETH, USDT], settings.market_data)
        self.fx = FxRateService(self.md, settings.fx)
        self.portfolio = FakePortfolio(equity)
        self.events = EventLog(tmp_path / "events.jsonl")
        self.state_path = tmp_path / "risk_state.json"
        self.seen = []
        self.events.subscribe(self.seen.append)
        self.engine = self.new_engine(**kw)

    def new_engine(self, **kw) -> RiskEngine:
        return RiskEngine(self.settings, self.md, self.fx, self.portfolio, self.events,
                          self.state_path, started_at=kw.get("started_at", T0 - timedelta(hours=1)))

    def kinds(self) -> list[str]:
        return [e.kind for e in self.seen]


@pytest.fixture
def rig(settings: Settings, tmp_path: Path) -> Rig:
    r = Rig(settings, tmp_path)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    return r


def good_intent(qty: str = "0.0009", stop: str = "3400000") -> TradeIntent:
    return TradeIntent(BTC, Decimal(qty), Decimal("3500000"), Decimal(stop))


def test_healthy_market_allows_small_trade(rig: Rig) -> None:
    decision = rig.engine.check_trade(good_intent(), T0)
    assert decision.allowed, decision.reasons
    # 0.0009 * (100 000 stop distance + 7 000 assumed costs) = 96.3 TL = 0.963% of 10 000
    assert decision.risk_amount == Decimal("96.3")


def test_per_trade_risk_limit(rig: Rig) -> None:
    big = rig.engine.check_trade(good_intent(qty="0.001"), T0)  # 107 TL = 1.07%
    assert not big.allowed
    assert any("İşlem riski çok yüksek" in r for r in big.reasons)
    qty = rig.engine.max_quantity(BTC, Decimal("3500000"), Decimal("3400000"))
    assert qty is not None
    qty = qty.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    assert rig.engine.check_trade(good_intent(qty=str(qty)), T0).allowed


def test_stop_is_required_and_spot_is_long_only(rig: Rig) -> None:
    assert not rig.engine.check_trade(good_intent(stop="3600000"), T0).allowed
    no_stop = TradeIntent(BTC, Decimal("0.001"), Decimal("3500000"), None)
    assert "Geçerli bir zarar-kes" in rig.engine.check_trade(no_stop, T0).reasons[0]
    short = TradeIntent(BTC, Decimal("0.001"), Decimal("3500000"), Decimal("3400000"), side="SHORT")
    assert not rig.engine.check_trade(short, T0).allowed


def test_max_concurrent_positions(rig: Rig) -> None:
    rig.portfolio.open = {ETH, USDT}
    decision = rig.engine.check_trade(good_intent(), T0)
    assert any("en fazla 2 pozisyon" in r for r in decision.reasons)
    rig.portfolio.open = {BTC}
    assert any("zaten açık" in r for r in rig.engine.check_trade(good_intent(), T0).reasons)


def test_stale_fx_blocks_new_positions(rig: Rig) -> None:
    later = T0 + timedelta(minutes=6)
    decision = rig.engine.check_trade(good_intent(), later)
    assert any("kuru 5 dakikadan eski" in r for r in decision.reasons)
    snap = rig.engine.evaluate(later)
    assert snap["new_positions_allowed"] is False
    assert "fx_stale" in rig.kinds()


def test_thin_book_wide_spread_low_volume_block_pair(settings: Settings, tmp_path: Path) -> None:
    r = Rig(settings, tmp_path)
    feed(r.md, T0, depth_try="1000")  # far below 250 000 TL
    t = make_ticker("BTC/TRY", "3500000", bid="3480000", ask="3520000")
    r.md.on_ticker(replace(t, volume_24h_quote=Decimal(1000), received_ts=T0))
    problems = r.engine.pair_problems(BTC, T0)
    joined = " | ".join(problems)
    assert "alış-satış farkı geniş" in joined
    assert "24 saatlik hacim düşük" in joined
    assert "emir defteri sığ" in joined
    assert not r.engine.check_trade(good_intent(), T0).allowed
    r.engine.evaluate(T0)
    assert "pair_blocked" not in r.kinds()  # brief flickers are not alerted
    later = T0 + timedelta(seconds=120)
    r.md.on_ticker(replace(t, volume_24h_quote=Decimal(1000), received_ts=later))
    feed_books_only = {s: replace(b, received_ts=later) for s, b in r.md._books.items()}
    r.md._books.update(feed_books_only)
    for sym in (ETH, USDT):
        r.md.on_ticker(replace(r.md.ticker(sym), received_ts=later))
    r.engine.evaluate(later)
    blocked = [e for e in r.seen if e.kind == "pair_blocked"]
    assert len(blocked) == 3 and blocked[0].level is Level.UYARI
    r.engine.evaluate(later)
    assert len([e for e in r.seen if e.kind == "pair_blocked"]) == 3  # not repeated
    # Recovery is announced once.
    feed(r.md, later + timedelta(seconds=5))
    r.engine.evaluate(later + timedelta(seconds=5))
    assert r.kinds().count("pair_ok") == 3


def test_no_pair_alerts_during_startup_grace(settings: Settings, tmp_path: Path) -> None:
    r = Rig(settings, tmp_path, started_at=T0)
    r.engine.evaluate(T0 + timedelta(seconds=10))  # no data yet, still in grace
    assert "pair_blocked" not in r.kinds() and "fx_stale" not in r.kinds()


def test_daily_loss_halts_until_next_day(rig: Rig) -> None:
    rig.portfolio.value = Decimal("9750")  # -2.5%: warning only
    rig.engine.evaluate(T0)
    assert "limit_near" in rig.kinds() and rig.engine.halt_level is HaltLevel.NORMAL
    rig.portfolio.value = Decimal("9700")  # -3%
    rig.engine.evaluate(T0)
    assert rig.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    crit = [e for e in rig.seen if e.kind == "daily_loss_halt"]
    assert len(crit) == 1 and crit[0].level is Level.KRITIK
    assert not rig.engine.check_trade(good_intent(), T0).allowed
    # /devam cannot lift it.
    assert "elle kaldırılamaz" in rig.engine.resume()
    assert rig.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    # Survives a restart.
    assert rig.new_engine().halt_level is HaltLevel.NO_NEW_POSITIONS
    # Turkey midnight = 21:00 UTC.
    next_day = datetime(2026, 10, 26, 21, 0, 1, tzinfo=timezone.utc)
    feed(rig.md, next_day)
    rig.engine.evaluate(next_day)
    assert rig.engine.halt_level is HaltLevel.NORMAL
    assert "daily_reset" in rig.kinds()


def test_weekly_loss_halt(settings: Settings, tmp_path: Path) -> None:
    r = Rig(settings, tmp_path)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    # Lose 2.9% on each of three days (each under the daily 3%), total > 6%.
    equity = Decimal(10000)
    for day in range(3):
        now = T0 + timedelta(days=day)
        feed(r.md, now)
        r.engine.evaluate(now)  # rolls the day
        equity *= Decimal("0.971")
        r.portfolio.value = equity
        r.engine.evaluate(now)
    assert any(h.kind == "weekly" for h in r.engine.halts())
    assert "weekly_loss_halt" in r.kinds()


def test_drawdown_kill_switch_needs_explicit_confirmation(rig: Rig) -> None:
    # Drawdown limit 10% is checked against the peak, across days.
    rig.portfolio.value = Decimal("12000")
    rig.engine.evaluate(T0)
    for day in range(1, 6):
        now = T0 + timedelta(days=day)
        feed(rig.md, now)
        rig.engine.evaluate(now)
        rig.portfolio.value -= Decimal("280")
        rig.engine.evaluate(now)
    assert rig.engine.halt_level is HaltLevel.CLOSE_ALL
    assert rig.engine.positions_must_close
    assert "drawdown_halt" in rig.kinds()
    msg = rig.engine.resume()
    assert "/devam ONAY" in msg and rig.engine.halt_level is HaltLevel.CLOSE_ALL
    rig.engine.resume(confirm=True)
    assert rig.engine.halt_level is not HaltLevel.CLOSE_ALL
    assert rig.engine.snapshot()["peak_equity"] == f"{rig.portfolio.value:.2f}"


def test_manual_stop_and_resume_persist(rig: Rig) -> None:
    assert "Durduruldu" in rig.engine.stop("Telegram /durdur")
    assert not rig.engine.check_trade(good_intent(), T0).allowed
    restarted = rig.new_engine()
    assert restarted.halt_level is HaltLevel.NO_NEW_POSITIONS
    assert "Kaldırıldı" in restarted.resume()
    assert restarted.halt_level is HaltLevel.NORMAL
    assert rig.new_engine().halt_level is HaltLevel.NORMAL


def test_delist_with_open_position_is_critical(rig: Rig) -> None:
    rig.portfolio.open = {ETH}
    rig.engine.set_live_pairs([BTC, USDT])
    rig.engine.evaluate(T0)
    crit = [e for e in rig.seen if e.kind == "delist_open_position"]
    assert len(crit) == 1 and crit[0].level is Level.KRITIK
    assert rig.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    assert any("listeden çıkmış" in p for p in rig.engine.pair_problems(ETH, T0))
    rig.engine.evaluate(T0)  # not repeated
    assert len([e for e in rig.seen if e.kind == "delist_open_position"]) == 1


def test_read_only_mode_never_trades(settings: Settings, tmp_path: Path) -> None:
    ro = settings.model_copy(update={"trading_mode": TradingMode.READ_ONLY})
    r = Rig(ro, tmp_path)
    feed(r.md, T0)
    assert not r.engine.check_trade(good_intent(), T0).allowed


def test_looser_limits_wait_for_approval_tighter_apply_at_once(settings: Settings, tmp_path: Path) -> None:
    Rig(settings, tmp_path)  # first run stores the approved limits
    looser = settings.model_copy(update={"risk": settings.risk.model_copy(
        update={"max_daily_loss_pct": Decimal(5), "max_concurrent_positions": 3})})
    r = Rig(looser, tmp_path)
    assert r.engine.limits.max_daily_loss_pct == Decimal(3)
    assert r.engine.limits.max_concurrent_positions == 2
    assert "limits_pending" in r.kinds()
    assert "onaylandı" in r.engine.approve_limits()
    assert r.engine.limits.max_daily_loss_pct == Decimal(5)

    tighter = settings.model_copy(update={"risk": settings.risk.model_copy(
        update={"max_daily_loss_pct": Decimal(2)})})
    t = Rig(tighter, tmp_path)
    assert t.engine.limits.max_daily_loss_pct == Decimal(2)
    assert "limits_changed" in t.kinds()


def test_corrupt_state_file_fails_safe(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "risk_state.json").write_text("{not json", encoding="utf-8")
    r = Rig(settings, tmp_path)
    assert r.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    assert r.seen[0].kind == "state_unreadable" and r.seen[0].level is Level.KRITIK


def test_limits_text_is_turkish(rig: Rig) -> None:
    text = rig.engine.limits_text()
    assert "Günlük zarar: %0,00 / %3,00" in text
    assert "Açık pozisyon: 0 / 2" in text


# ----------------------------------------------------------------- capital guard

@pytest.fixture
def guarded(settings: Settings, tmp_path: Path) -> Rig:
    r = Rig(settings, tmp_path, guard=True)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    return r


def test_capital_floor_starts_just_under_the_money_put_in(guarded: Rig) -> None:
    assert guarded.engine.capital_floor() == Decimal("9800")  # 10,000 TL - 2%
    guard = guarded.engine.snapshot(T0)["capital_guard"]
    assert guard["floor"] == "9800.00" and guard["headroom"] == "200.00" and guard["used_pct"] == "0.00"
    assert "Ana para tabanı" in guarded.engine.limits_text()


def test_capital_floor_rises_with_profit_and_never_falls(guarded: Rig) -> None:
    guarded.portfolio.value = Decimal("11000")
    guarded.engine.evaluate(T0)
    assert guarded.engine.capital_floor() == Decimal("10300")  # 9,800 + half of the 1,000 gain
    guarded.portfolio.value = Decimal("10500")
    guarded.engine.evaluate(T0)
    assert guarded.engine.capital_floor() == Decimal("10300")


def test_falling_to_the_floor_closes_everything_until_owner_confirms(guarded: Rig) -> None:
    guarded.portfolio.value = Decimal("9790")
    guarded.engine.evaluate(T0)
    assert guarded.engine.halt_level is HaltLevel.CLOSE_ALL
    assert "capital_floor_halt" in guarded.kinds()
    assert "sahibi" in guarded.engine.resume(by="jarvis")
    assert "ONAY" in guarded.engine.resume()
    assert guarded.engine.positions_must_close
    guarded.engine.resume(confirm=True)
    assert not any(h.kind == "floor" for h in guarded.engine.halts())
    assert guarded.engine.capital_floor() == Decimal("9790") * Decimal("0.98")  # rebuilt from today


def test_trade_size_keeps_every_open_trade_above_the_floor(guarded: Rig) -> None:
    # 9,850 TL: 50 TL above the floor, shared by the 2 free slots -> 25 TL risk per trade,
    # counting costs (0.2%) and a possible gap past the stop (0.5%).
    guarded.portfolio.value = Decimal("9850")
    guarded.engine.evaluate(T0)
    qty = guarded.engine.max_quantity(BTC, Decimal("3500000"), Decimal("3400000"))
    per_unit = Decimal("3500000") - Decimal("3400000") + Decimal("3500000") * 70 / 10_000
    assert qty * per_unit == pytest.approx(Decimal(25))
    big = guarded.engine.check_trade(TradeIntent(BTC, Decimal("0.001"), Decimal("3500000"), Decimal("3400000")), T0)
    assert not big.allowed and any("Ana para koruması" in r for r in big.reasons)
    ok = guarded.engine.check_trade(TradeIntent(BTC, qty, Decimal("3500000"), Decimal("3400000")), T0)
    assert ok.allowed, ok.reasons


def test_no_buy_while_sellers_dominate_the_book(rig: Rig) -> None:
    p = Decimal(PRICES[BTC])
    rig.md._books[BTC] = OrderBook(BTC, (BookLevel(p, Decimal("0.05")),), (BookLevel(p + 1, Decimal("0.5")),),
                                   exchange_ts=T0, received_ts=T0)
    decision = rig.engine.check_trade(good_intent(), T0)
    assert any("satıcılar baskın" in r for r in decision.reasons)


def test_three_losses_in_a_row_pause_new_trades_for_a_day(settings: Settings, tmp_path: Path) -> None:
    r = Rig(settings, tmp_path, guard=True)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    for pnl in ("-10", "-10", "+5", "-10", "-10"):  # a win resets the count
        r.events.emit(Level.BILGI, "trade_closed", "x", pnl_try=Decimal(pnl))
    assert r.engine.halt_level is HaltLevel.NORMAL
    r.events.emit(Level.UYARI, "stop_loss_hit", "x", pnl_try=Decimal("-10"))
    assert "loss_streak_pause" in r.kinds()
    assert r.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    assert "elle kaldırılamaz" in r.engine.resume()
    assert r.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    # Survives a restart, ends by itself after 24 hours.
    r.engine = r.new_engine()
    assert any(h.kind == "streak" for h in r.engine.halts())
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    feed(r.md, later)
    r.engine.evaluate(later)
    assert not any(h.kind == "streak" for h in r.engine.halts())
    assert "loss_pause_over" in r.kinds()
