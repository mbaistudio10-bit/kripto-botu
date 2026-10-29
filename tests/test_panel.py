"""Control Center (panel) API, health score, Jarvis overview, Telegram /panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cryptobot.api.jarvis import JarvisApi
from cryptobot.api.panel import PanelApi, backtest_summary, build_overview, panel_links
from cryptobot.config import Settings
from cryptobot.events import emit_trade_closed
from cryptobot.health import TARGET, Links, health
from cryptobot.risk.engine import HaltLevel
from cryptobot.status import build_snapshot
from tests.test_notify import CONTROL_KEY, READ_KEY, auth
from tests.test_risk_engine import T0, Rig

PANEL_KEY = "p" * 32


@pytest.fixture
def rig(settings: Settings, tmp_path: Path) -> Rig:
    from tests.test_risk_engine import feed

    r = Rig(settings, tmp_path)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    return r


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def panel(rig: Rig):
    clock = Clock()
    api = PanelApi(rig.engine, rig.events, lambda: {"mode": "PAPER_TRADING"}, PANEL_KEY, clock=clock)
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    client.clock = clock  # type: ignore[attr-defined]
    yield client
    await client.close()


async def test_page_and_app_files_are_public_but_data_needs_the_key(panel: TestClient) -> None:
    page = await panel.get("/")
    text = await page.text()
    assert page.status == 200 and "Kontrol Merkezi" in text and 'rel="manifest"' in text
    assert page.headers["X-Frame-Options"] == "DENY"
    manifest = await (await panel.get("/manifest.webmanifest")).json(content_type=None)
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert (await panel.get("/sw.js")).status == 200
    assert (await panel.get("/icon.svg")).status == 200
    assert (await panel.get("/panel/api/state")).status == 401
    assert (await panel.get("/panel/api/state", headers=auth(READ_KEY))).status == 401
    resp = await panel.get("/panel/api/state", headers=auth(PANEL_KEY))
    body = await resp.json()
    assert resp.status == 200 and body["mode"] == "PAPER_TRADING"
    assert set(body["reports"]) == {"gün", "hafta", "ay"} and "limits_text" in body


async def test_page_never_inserts_bot_text_as_html() -> None:
    from cryptobot.api.panel_page import PAGE_HTML

    script = PAGE_HTML.split("<script>", 1)[1]
    # innerHTML is only used once, for the fixed nav icons.
    assert script.count("innerHTML") == 1 and "b.innerHTML='<svg" in script


async def test_wrong_keys_lock_the_address_out(panel: TestClient) -> None:
    for _ in range(10):
        assert (await panel.get("/panel/api/state", headers=auth("x" * 32))).status == 401
    locked = await panel.get("/panel/api/state", headers=auth(PANEL_KEY))
    assert locked.status == 429  # even the right key, until the lockout ends
    panel.clock.t += 16 * 60  # type: ignore[attr-defined]
    assert (await panel.get("/panel/api/state", headers=auth(PANEL_KEY))).status == 200


async def test_panel_stops_and_resumes_like_the_owner(rig: Rig, panel: TestClient) -> None:
    resp = await panel.post("/panel/api/stop", headers=auth(PANEL_KEY), json={"reason": "test"})
    assert (await resp.json())["halt_level"] == "NO_NEW_POSITIONS"
    stop = [e for e in rig.seen if e.kind == "manual_stop"][-1]
    assert "Kontrol Merkezi" in stop.message and stop.should_notify
    # Jarvis cannot lift the owner's stop made from the panel.
    assert "sahibi" in rig.engine.resume(by="jarvis")
    resp = await panel.post("/panel/api/resume", headers=auth(PANEL_KEY))
    assert (await resp.json())["halt_level"] == "NORMAL"
    assert any(e.kind == "resumed" and "Kontrol Merkezi" in e.message for e in rig.seen)


async def test_drawdown_halt_needs_explicit_confirm(rig: Rig, panel: TestClient) -> None:
    rig.portfolio.value = Decimal("8900")  # -11%
    rig.engine.evaluate(T0)
    assert rig.engine.halt_level is HaltLevel.CLOSE_ALL
    resp = await panel.post("/panel/api/resume", headers=auth(PANEL_KEY), json={"confirm": "yes"})
    assert "ONAY" in (await resp.json())["message"] and rig.engine.halt_level is HaltLevel.CLOSE_ALL
    await panel.post("/panel/api/resume", headers=auth(PANEL_KEY), json={"confirm": True})
    # The drawdown halt is lifted; the daily and weekly loss halts stay until they expire.
    assert rig.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    assert {h.kind for h in rig.engine.halts()} == {"daily", "weekly"}


async def test_panel_has_no_order_or_limit_endpoints(panel: TestClient) -> None:
    for path in ("/panel/api/order", "/panel/api/limits", "/panel/api/approve"):
        assert (await panel.post(path, headers=auth(PANEL_KEY))).status == 404


def test_panel_key_must_be_long(rig: Rig) -> None:
    with pytest.raises(ValueError):
        PanelApi(rig.engine, rig.events, dict, "short")


def test_links_keep_the_key_out_of_server_logs() -> None:
    links = panel_links("k" * 32, 8770, home_network=False)
    assert links == [("Bu bilgisayarda", f"http://localhost:8770/#k={'k' * 32}")]


def test_backtest_summary_drops_the_details(tmp_path: Path) -> None:
    path = tmp_path / "r.txt"
    assert backtest_summary(path) is None
    path.write_text("📈 özet\nSonuç: x\n\n\nAYRINTILAR\n\nuzun", encoding="utf-8")
    assert backtest_summary(path) == "📈 özet\nSonuç: x"


async def test_jarvis_overview_is_the_panel_view(rig: Rig) -> None:
    emit_trade_closed(rig.events, trade_id="a", symbol="BTC/USDT", quantity=Decimal("0.001"),
                      price=Decimal("100000"), pnl_try=Decimal("50"), pnl_usd=Decimal("1.4"))
    overview = lambda: build_overview(rig.engine, rig.events, lambda: {"health": {"score": 9}}, 3)  # noqa: E731
    api = JarvisApi(rig.engine, rig.events, dict, READ_KEY, CONTROL_KEY, overview=overview)
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    try:
        assert (await client.get("/api/v1/overview")).status == 401
        body = await (await client.get("/api/v1/overview", headers=auth(READ_KEY))).json()
    finally:
        await client.close()
    assert body["health"] == {"score": 9}
    assert body["reports"]["gün"]["categories"]["Kazanç"]["pnl_try"] == "50.00"
    assert body["events"][0]["kind"] == "trade_closed"


# ----------------------------------------------------------------- health score

def _snap(rig: Rig, settings: Settings) -> dict:
    snap = build_snapshot(settings, rig.md, rig.fx, T0, rig.engine.snapshot(T0), None)
    for p in snap["pairs"]:
        p["stale"] = False  # the fake feed is at T0; treat it as live
    if snap["fx"]:
        snap["fx"]["fresh"] = True
    return snap


def test_health_full_marks_when_everything_is_set_up(rig: Rig, settings: Settings) -> None:
    s = settings.model_copy(update={"strategy": settings.strategy.model_copy(update={"enabled": False})})
    links = Links(telegram_token=True, telegram_chat=True, jarvis_api=True)
    h = health(s, _snap(rig, s), links, now=T0)
    parts = {p["key"]: p for p in h["parts"]}
    assert parts["protection"]["score"] == 10 and parts["headroom"]["score"] == 10
    assert parts["market"]["score"] == 10 and parts["fx"]["score"] == 10
    assert parts["telegram"]["score"] == 10 and parts["jarvis"]["score"] == 10
    assert parts["strategy"]["score"] == 7  # switched off on purpose
    assert h["score"] >= TARGET and h["pct"] == round(h["score"] * 10)
    assert "kâr garantisi değildir" in h["note"]


def test_health_drops_for_missing_links_losses_and_loose_limits(rig: Rig, settings: Settings) -> None:
    loose = settings.risk.model_copy(update={"max_daily_loss_pct": Decimal(5)})
    s = settings.model_copy(update={"risk": loose})
    rig.portfolio.value = Decimal("9850")  # -1.5%: half the 3% daily limit
    rig.engine.evaluate(T0)
    snap = _snap(rig, s)
    snap["pairs"][0]["stale"] = True
    h = health(s, snap, Links(), now=T0)
    parts = {p["key"]: p for p in h["parts"]}
    assert parts["protection"]["score"] == 8 and "gevşetilmiş" in parts["protection"]["note"]
    assert parts["headroom"]["score"] == pytest.approx(5, abs=0.1)
    assert parts["market"]["score"] < 10 and "Canlı değil" in parts["market"]["note"]
    assert parts["telegram"]["score"] == 0 and parts["jarvis"]["score"] == 3
    assert not h["ok"] and "Hedefin altında" in h["summary"]


def test_health_strategy_needs_fresh_candles(rig: Rig, settings: Settings) -> None:
    snap = _snap(rig, settings)
    fresh = (T0 - timedelta(hours=4)).isoformat()
    old = (T0 - timedelta(days=2)).isoformat()
    snap["strategy"] = {"pairs": {"BTC/USDT": {"ready": True, "last_candle": fresh},
                                  "ETH/USDT": {"ready": True, "last_candle": old}}}
    part = {p["key"]: p for p in health(settings, snap, Links(), now=T0)["parts"]}["strategy"]
    assert part["score"] == 5 and "ETH/USDT" in part["note"]


def test_telegram_panel_command(rig: Rig, settings: Settings) -> None:
    from cryptobot.notify.telegram import TelegramBot

    bot = TelegramBot(settings.telegram, None, 1, rig.engine, rig.events,  # type: ignore[arg-type]
                      status_text=str, report_text=str, panel_text=lambda: "links here")
    assert bot.handle_command("/panel") == "links here"
    assert "/panel" in bot.handle_command("/yardim")
    off = TelegramBot(settings.telegram, None, 1, rig.engine, rig.events,  # type: ignore[arg-type]
                      status_text=str, report_text=str)
    assert "kapalı" in off.handle_command("/panel")
    assert datetime.now(timezone.utc)  # keeps the import used
