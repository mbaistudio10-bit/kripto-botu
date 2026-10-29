from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cryptobot.config import Settings
from cryptobot.domain.models import Instrument, MarketType, Symbol
from cryptobot.events import STOP_LOSS_HIT, TRADE_OPENED
from cryptobot.exchanges.base import ExchangeError
from cryptobot.exchanges.okx_private import (
    Balance, OkxCredentials, OkxPrivateClient, OrderResult, ceil_to_step, floor_to_step, load_credentials,
)
from cryptobot.live.trader import LiveTrader
from tests.test_paper import BTC, Rig, set_price

CRED = OkxCredentials("KEY", "SECRET", "PASS")


# ------------------------------------------------------------------ signed client

class Capture:
    def __init__(self, replies: list[tuple[int, dict]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload)


def ok(data: list) -> tuple[int, dict]:
    return 200, {"code": "0", "msg": "", "data": data}


def test_request_is_signed_over_timestamp_method_path_and_body() -> None:
    cap = Capture([ok([{"ordId": "42", "sCode": "0"}])])
    client = OkxPrivateClient("https://tr.okx.com/", CRED, transport=cap)
    assert client.place_order("BTC-USDT", "buy", "ioc", Decimal("0.001"), Decimal("80000.1"), "kb1") == "42"
    method, url, headers, body = cap.calls[0]
    assert method == "POST" and url == "https://tr.okx.com/api/v5/trade/order"
    expected = base64.b64encode(hmac.new(
        b"SECRET", (headers["OK-ACCESS-TIMESTAMP"] + "POST/api/v5/trade/order" + body.decode()).encode(),
        hashlib.sha256).digest()).decode()
    assert headers["OK-ACCESS-SIGN"] == expected
    assert headers["OK-ACCESS-KEY"] == "KEY" and headers["OK-ACCESS-PASSPHRASE"] == "PASS"
    sent = json.loads(body)
    assert sent == {"instId": "BTC-USDT", "tdMode": "cash", "side": "buy", "ordType": "ioc",
                    "sz": "0.001", "px": "80000.1", "clOrdId": "kb1"}


def test_market_sell_is_sized_in_base_currency_and_query_is_signed() -> None:
    cap = Capture([ok([{"ordId": "7", "sCode": "0"}]), ok([])])
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=cap)
    client.place_order("BTC-USDT", "sell", "market", Decimal("0.00000001"))
    sent = json.loads(cap.calls[0][3])
    assert sent["tgtCcy"] == "base_ccy" and sent["sz"] == "0.00000001" and "px" not in sent
    client.get_order("BTC-USDT", order_id="7")
    _, url, headers, _ = cap.calls[1]
    assert url.endswith("/api/v5/trade/order?instId=BTC-USDT&ordId=7")
    prehash = headers["OK-ACCESS-TIMESTAMP"] + "GET/api/v5/trade/order?instId=BTC-USDT&ordId=7"
    assert headers["OK-ACCESS-SIGN"] == base64.b64encode(
        hmac.new(b"SECRET", prehash.encode(), hashlib.sha256).digest()).decode()


def test_errors_never_leak_keys_and_order_refusals_raise() -> None:
    cap = Capture([(200, {"code": "1", "msg": "All operations failed",
                          "data": [{"sCode": "51008", "sMsg": "Insufficient balance"}]})])
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=cap)
    with pytest.raises(ExchangeError, match="Insufficient balance") as exc:
        client.place_order("BTC-USDT", "buy", "ioc", Decimal(1), Decimal(1))
    assert "SECRET" not in str(exc.value) and "PASS" not in str(exc.value)
    assert "SECRET" not in repr(CRED)


def test_permissions_and_balances_are_parsed() -> None:
    cap = Capture([ok([{"perm": "read_only,trade"}]),
                   ok([{"details": [{"ccy": "USDT", "availBal": "8.5", "cashBal": "9"},
                                    {"ccy": "BTC", "availBal": "0", "cashBal": "0"}]}])])
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=cap)
    assert client.key_permissions() == {"read_only", "trade"}
    bal = client.balances()
    assert bal == {"USDT": Balance(Decimal("8.5"), Decimal("9"))}


def test_get_order_parses_fill_and_fee() -> None:
    row = {"ordId": "9", "state": "filled", "accFillSz": "0.001", "avgPx": "80000", "fee": "-0.000001", "feeCcy": "btc"}
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=Capture([ok([row])]))
    res = client.get_order("BTC-USDT", order_id="9")
    assert res == OrderResult("9", "filled", Decimal("0.001"), Decimal(80000), Decimal("0.000001"), "BTC")
    assert res.finished


def test_wait_order_cancels_an_order_that_stays_open() -> None:
    live = {"ordId": "9", "state": "live", "accFillSz": "0", "avgPx": "", "fee": "", "feeCcy": ""}
    cancelled = {**live, "state": "canceled"}
    cap = Capture([ok([live]), ok([live]), ok([live]), ok([{"ordId": "9"}]), ok([cancelled])])
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=cap, sleep=lambda s: None)
    res = client.wait_order("BTC-USDT", "9", seconds=0.5)
    assert res.state == "canceled"
    assert any(c[1].endswith("/cancel-order") for c in cap.calls)


def test_credentials_names_and_missing() -> None:
    cred, missing = load_credentials({"OKX_API_KEY": "a", "OKX_SECRET_KEY": "b", "OKX_PASSPHRASE": "c"})
    assert cred == OkxCredentials("a", "b", "c") and missing == []
    cred, missing = load_credentials({"OKX_API_KEY": "a"})
    assert cred is None and missing == ["OKX_API_SECRET", "OKX_API_PASSPHRASE"]


def test_step_rounding() -> None:
    assert floor_to_step(Decimal("0.123456789"), Decimal("0.00000001")) == Decimal("0.12345678")
    assert ceil_to_step(Decimal("80000.01"), Decimal("0.1")) == Decimal("80000.1")


# ------------------------------------------------------------------ live trader

class FakeClient:
    """Stands in for the exchange: fills at the price the test sets, takes OKX-style fees."""

    def __init__(self, balances: dict[str, str]) -> None:
        self.bal = {k: Balance(Decimal(v), Decimal(v)) for k, v in balances.items()}
        self.orders: list[dict] = []
        self.price = Decimal(0)
        self.fill_ratio = Decimal(1)
        self.fail = False

    def balances(self) -> dict[str, Balance]:
        return self.bal

    def place_order(self, inst_id, side, ord_type, size, price=None, client_id=None) -> str:
        if self.fail and side == "sell":
            raise ExchangeError("boom")
        self.orders.append({"inst": inst_id, "side": side, "type": ord_type, "size": size, "price": price})
        return str(len(self.orders))

    def get_order(self, inst_id, order_id=None, client_id=None):
        return None

    def wait_order(self, inst_id, order_id, seconds) -> OrderResult:
        o = self.orders[int(order_id) - 1]
        filled = o["size"] * self.fill_ratio
        if o["side"] == "buy":  # fee taken in the coin, like OKX spot
            return OrderResult(order_id, "filled", filled, self.price, filled * Decimal("0.001"), "BTC")
        return OrderResult(order_id, "filled", filled, self.price, filled * self.price * Decimal("0.001"), "TRY")


class LiveRig(Rig):
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        super().__init__(settings, tmp_path)
        live_cfg = settings.live.model_copy(update={"confirmed": True})
        inst = {BTC: Instrument(BTC, "BTC-TRY", MarketType.SPOT, Decimal(1), Decimal("0.00000001"),
                                Decimal("0.00001"), True)}
        self.client = FakeClient({"TRY": "10000"})
        self.client.price = Decimal(3500000)
        self.trader = LiveTrader(self.portfolio, self.engine, self.md, self.events, self.paper_cfg,
                                 live_cfg, self.client, inst)  # type: ignore[arg-type]


@pytest.fixture
def lrig(settings: Settings, tmp_path: Path) -> LiveRig:
    return LiveRig(settings, tmp_path)


def test_real_buy_books_the_real_fill_and_fee(lrig: LiveRig) -> None:
    assert lrig.buy(note="deneme") == []
    order = lrig.client.orders[0]
    assert order["side"] == "buy" and order["type"] == "ioc"
    assert order["price"] == Decimal("3510500")  # ask 3.5M + 30 bps, to the tick
    pos = lrig.portfolio.positions[BTC]
    gross = order["size"]
    assert pos.quantity == gross - gross * Decimal("0.001")  # the coin fee leaves fewer coins
    assert pos.cost == pytest.approx(gross * Decimal(3500000))  # money paid = gross * price
    opened = [e for e in lrig.seen if e.kind == TRADE_OPENED][0]
    assert "GERÇEK PARA" in opened.message and opened.data["paper"] is False


def test_buy_refused_when_the_exchange_account_is_short(lrig: LiveRig) -> None:
    lrig.client.bal = {"TRY": Balance(Decimal(10), Decimal(10))}
    reasons = lrig.buy()
    assert reasons and "yeterli TRY yok" in reasons[0]
    assert lrig.client.orders == [] and not lrig.portfolio.positions


def test_unfilled_buy_spends_nothing(lrig: LiveRig) -> None:
    lrig.client.fill_ratio = Decimal(0)
    reasons = lrig.buy()
    assert "dolmadı" in reasons[0] and not lrig.portfolio.positions
    assert lrig.portfolio.balances["TRY"] == Decimal(10000)


def test_stop_loss_sells_for_real_with_a_market_order(lrig: LiveRig) -> None:
    lrig.buy()
    held = lrig.portfolio.positions[BTC].quantity
    lrig.client.bal["BTC"] = Balance(held, held)
    lrig.client.price = Decimal(3460000)
    set_price(lrig.md, BTC, "3460000", lrig.now)
    lrig.trader.step(lrig.now)
    sell = lrig.client.orders[-1]
    assert sell["side"] == "sell" and sell["type"] == "market"
    assert sell["size"] == floor_to_step(held, Decimal("0.00000001"))
    assert not lrig.portfolio.positions
    hit = [e for e in lrig.seen if e.kind == STOP_LOSS_HIT]
    assert len(hit) == 1 and hit[0].data["pnl_try"] < 0 and hit[0].data["paper"] is False


def test_failed_sell_keeps_the_position_alerts_and_waits_before_retrying(lrig: LiveRig) -> None:
    lrig.buy()
    held = lrig.portfolio.positions[BTC].quantity
    lrig.client.bal["BTC"] = Balance(held, held)
    lrig.client.fail = True
    set_price(lrig.md, BTC, "3460000", lrig.now)
    lrig.trader.step(lrig.now)
    assert BTC in lrig.portfolio.positions
    assert any(e.kind == "live_order_problem" for e in lrig.seen)
    orders = len(lrig.client.orders)
    lrig.client.fail = False
    lrig.trader.step(lrig.now + timedelta(seconds=5))  # too soon: no new attempt
    assert len(lrig.client.orders) == orders and BTC in lrig.portfolio.positions
    later = lrig.now + timedelta(seconds=31)
    set_price(lrig.md, BTC, "3460000", later)
    lrig.client.price = Decimal(3460000)
    lrig.trader.step(later)
    assert BTC not in lrig.portfolio.positions


def test_reconcile_flags_coins_missing_from_the_account(lrig: LiveRig) -> None:
    lrig.buy()
    held = lrig.portfolio.positions[BTC].quantity
    lrig.client.bal["BTC"] = Balance(held, held)
    assert lrig.trader.reconcile() == []
    lrig.client.bal["BTC"] = Balance(Decimal(0), Decimal(0))  # sold by hand in the app
    assert "BTC" in lrig.trader.reconcile()[0]


def test_daily_buy_limit(lrig: LiveRig) -> None:
    cfg = lrig.settings.live.model_copy(update={"max_buys_per_day": 1})
    lrig.trader._live = cfg
    assert lrig.buy() == []
    held = lrig.portfolio.positions[BTC].quantity
    lrig.client.bal["BTC"] = Balance(held, held)
    lrig.trader.close(BTC, "satış", None, lrig.now)
    assert "alış sınırı" in lrig.buy()[0]


def test_idle_cash_is_never_converted_in_live(lrig: LiveRig) -> None:
    lrig.portfolio.balances["USDT"] = Decimal(5)
    lrig.trader.step(lrig.now)
    assert lrig.portfolio.balances.get("USDT") == Decimal(5)


# ------------------------------------------------------------------ start-up checks

from cryptobot.live import setup as live_setup  # noqa: E402
from tests.conftest import FakeAdapter  # noqa: E402


def _live_settings(settings: Settings, **live: object) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["pairs"] = [str(p) for p in settings.pairs]
    raw["fx"]["source_pair"] = str(settings.fx.source_pair)
    raw["trading_mode"] = "LIVE_TRADING"
    raw["live"].update(live)
    return Settings.model_validate(raw)


def _patch_client(monkeypatch: pytest.MonkeyPatch, replies: list[tuple[int, dict]]) -> None:
    client = OkxPrivateClient("https://tr.okx.com", CRED, transport=Capture(replies))
    monkeypatch.setattr(live_setup, "make_client", lambda s: client)


def _adapter(settings: Settings) -> tuple[FakeAdapter, list[Symbol]]:
    prices = {str(p): "100" for p in settings.pairs}
    prices["USDT/TRY"] = "50"
    return FakeAdapter(prices), list(settings.pairs)


async def test_live_refuses_to_start_unless_confirmed(settings: Settings) -> None:
    adapter, pairs = _adapter(settings)
    assert await live_setup.prepare_live(adapter, _live_settings(settings), pairs, False) is None


async def test_live_refuses_a_key_that_can_withdraw(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [ok([{"perm": "read_only,trade,withdraw"}]), ok([{"details": []}])])
    adapter, pairs = _adapter(settings)
    assert await live_setup.prepare_live(adapter, _live_settings(settings, confirmed=True), pairs, False) is None


async def test_live_start_money_is_the_smaller_of_balance_and_cap(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    balance = ok([{"details": [{"ccy": "USDT", "availBal": "50", "cashBal": "50"}]}])
    _patch_client(monkeypatch, [ok([{"perm": "read_only,trade"}]), balance])
    adapter, pairs = _adapter(settings)
    cfg = _live_settings(settings, confirmed=True, max_capital_try=400)
    ctx = await live_setup.prepare_live(adapter, cfg, pairs, False)
    assert ctx is not None
    assert ctx.start_cash == Decimal(8)  # 400 TL at 50 TL per USDT, although 50 USDT are in the account


async def test_live_needs_cash_in_the_account(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [ok([{"perm": "read_only,trade"}]), ok([{"details": []}])])
    adapter, pairs = _adapter(settings)
    assert await live_setup.prepare_live(adapter, _live_settings(settings, confirmed=True), pairs, False) is None


def test_canli_overlay_switches_to_live_and_okx_tr_with_its_own_files() -> None:
    from cryptobot.config import load_settings
    from tests.conftest import ROOT

    cfg = load_settings(ROOT / "config" / "settings.yaml", ROOT / "config" / "canli.yaml")
    assert cfg.trading_mode.value == "LIVE_TRADING"
    assert cfg.exchange.rest_base_url == "https://tr.okx.com" and cfg.market_data.mode == "rest"
    assert cfg.live.confirmed is False and cfg.live.max_capital_try == 400
    assert cfg.status.events_file.name == "events_live.jsonl"
    assert cfg.status.risk_state_file.name == "risk_state_live.json"
    # the normal file stays on practice money
    assert load_settings(ROOT / "config" / "settings.yaml").trading_mode.value == "PAPER_TRADING"


def test_buy_too_close_to_the_exchange_minimum_is_refused(lrig: LiveRig) -> None:
    # 0.00001 is the exchange minimum; the coin fee would leave 0.00000999, which cannot be sold.
    assert "satılamaz" in lrig.trader._check_order_size(BTC, Decimal("0.00001"))[0]
    assert lrig.trader._check_order_size(BTC, Decimal("0.0000101")) == []


class _SelftestAdapter(FakeAdapter):
    async def fetch_instruments(self, market_type):
        return [Instrument(Symbol.parse(p), p.replace("/", "-"), market_type, Decimal("0.1"),
                           Decimal("0.00000001"), Decimal("0.00001"), True) for p in self.prices]


class _SelftestClient(FakeClient):
    """Behaves like the real OKX TR did in the first live test: sells under the minimum size are refused."""

    def key_permissions(self) -> set[str]:
        return {"read_only", "trade"}

    def place_order(self, inst_id, side, ord_type, size, price=None, client_id=None) -> str:
        if size < Decimal("0.00001"):
            raise ExchangeError("OKX error: Your order should meet or exceed the minimum order amount.")
        return super().place_order(inst_id, side, ord_type, size, price, client_id)

    def wait_order(self, inst_id, order_id, seconds) -> OrderResult:
        o = self.orders[int(order_id) - 1]
        if o["side"] == "buy":
            return OrderResult(order_id, "filled", o["size"], self.price, o["size"] * Decimal("0.001"), "BTC")
        return OrderResult(order_id, "filled", o["size"], self.price, o["size"] * self.price * Decimal("0.001"), "USDT")


async def test_selftest_buys_above_the_minimum_so_it_can_sell_again_and_sweeps_old_dust(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _SelftestClient({"USDT": "7"})
    client.bal["BTC"] = Balance(Decimal("0.00000999"), Decimal("0.00000999"))  # left by the first failed test
    client.price = Decimal(79596)
    monkeypatch.setattr(live_setup, "make_client", lambda s: client)
    cfg = _live_settings(settings, confirmed=True)
    adapter = _SelftestAdapter({"BTC/USDT": "79596", "ETH/USDT": "2500", "BTC/TRY": "3900000",
                                "ETH/TRY": "120000", "USDT/TRY": "49.6"})
    code = await live_setup.run_selftest(adapter, cfg, Decimal(30), ask=lambda _: "EVET")
    assert code == 0
    buy, sell = client.orders
    assert buy["size"] >= Decimal("0.00001") * Decimal("1.01")
    # the sale includes the old dust: bought net of fee + 0.00000999
    assert sell["side"] == "sell" and sell["size"] > buy["size"]
