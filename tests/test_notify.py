"""Telegram (Achilles) alerts/commands, Jarvis API, events, report, .env handling."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cryptobot.api.jarvis import JarvisApi, JarvisWebhook
from cryptobot.config import Settings
from cryptobot.envfile import ensure_keys, load_env
from cryptobot.events import (
    ErrorEventHandler, EventLog, Level, emit_trade_closed, emit_trade_opened,
)
from cryptobot.notify.telegram import TelegramApi, TelegramBot, TelegramError
from cryptobot.report import build_report, period_start
from cryptobot.risk.engine import HaltLevel
from tests.test_risk_engine import T0, Rig, feed

TOKEN = "123456:TEST-token-not-real"
OWNER = 111222333
READ_KEY = "r" * 32
CONTROL_KEY = "c" * 32


class FakeTelegram:
    """Records sendMessage calls and serves queued getUpdates."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.updates: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()

        async def handler(request: web.Request) -> web.Response:
            token, method = request.match_info["token"], request.match_info["method"]
            if token != f"bot{TOKEN}":
                return web.json_response({"ok": False, "description": "Unauthorized"}, status=401)
            body = await request.json()
            if method == "sendMessage":
                self.sent.append(body)
                return web.json_response({"ok": True, "result": {}})
            if method == "getUpdates":
                offset = body.get("offset") or 0
                result = [u for u in self.updates if u["update_id"] >= offset]
                if not result and body.get("timeout"):
                    await asyncio.sleep(0.05)
                return web.json_response({"ok": True, "result": result})
            return web.json_response({"ok": False, "description": "Not Found"}, status=404)

        app.router.add_post("/{token}/{method}", handler)
        return app


def msg(update_id: int, text: str, chat_id: int = OWNER, is_bot: bool = False) -> dict:
    return {"update_id": update_id, "message": {
        "text": text, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": is_bot, "first_name": "Mahmut"}}}


@pytest.fixture
def rig(settings: Settings, tmp_path: Path) -> Rig:
    r = Rig(settings, tmp_path)
    feed(r.md, T0)
    r.engine.evaluate(T0)
    return r


@pytest.fixture
async def tg(rig: Rig):
    fake = FakeTelegram()
    server = TestServer(fake.app())
    await server.start_server()
    session = aiohttp.ClientSession()
    api = TelegramApi(str(server.make_url("")), TOKEN, session)
    printed: list[str] = []
    bot = TelegramBot(rig.settings.telegram, api, OWNER, rig.engine, rig.events,
                      status_text=lambda: "durum-metni", report_text=lambda p: f"rapor-{p}",
                      console=printed.append)
    yield fake, bot, api, printed
    await session.close()
    await server.close()


async def _drain(bot: TelegramBot, rounds: int = 20) -> None:
    task = asyncio.create_task(bot._sender())
    for _ in range(rounds):
        await asyncio.sleep(0.01)
    task.cancel()


async def test_alerts_go_to_owner_with_cooldown_but_trades_always(rig: Rig, tg) -> None:
    fake, bot, _, _ = tg
    rig.events.emit(Level.UYARI, "fx_stale", "kur eski")
    rig.events.emit(Level.UYARI, "fx_stale", "kur eski")  # same key within 30 min: dropped
    rig.events.emit(Level.BILGI, "started", "sessiz")  # BILGI is not pushed by default
    rig.events.emit(Level.KRITIK, "daily_loss_halt", "limit")
    rig.events.emit(Level.KRITIK, "daily_loss_halt", "limit")  # KRITIK is never dropped
    for i in range(2):
        emit_trade_opened(rig.events, trade_id=f"t{i}", symbol="BTC/TRY", quantity=Decimal("0.001"),
                          price=Decimal("3500000"), value_try=Decimal("3500"), stop_price=Decimal("3400000"))
    emit_trade_closed(rig.events, trade_id="t0", symbol="BTC/TRY", quantity=Decimal("0.001"),
                      price=Decimal("3400000"), pnl_try=Decimal("-100"), pnl_usd=Decimal("-2.86"),
                      reason="stop-loss")
    await _drain(bot)
    texts = [m["text"] for m in fake.sent]
    assert all(m["chat_id"] == OWNER for m in fake.sent)
    assert sum("kur eski" in t for t in texts) == 1
    assert sum("KRİTİK" in t for t in texts) == 2
    assert not any("sessiz" in t for t in texts)
    assert sum("ALIŞ (deneme): BTC/TRY" in t for t in texts) == 2
    assert any("STOP-LOSS ÇALIŞTI" in t and "-100,00 TL" in t for t in texts)


async def test_system_errors_reach_telegram(rig: Rig, tg) -> None:
    fake, bot, _, _ = tg
    logger = logging.getLogger("cryptobot.test")
    handler = ErrorEventHandler(rig.events)
    logger.addHandler(handler)
    try:
        logger.error("database exploded")
        logger.warning("just a warning")
    finally:
        logger.removeHandler(handler)
    await _drain(bot)
    texts = [m["text"] for m in fake.sent]
    assert any("Sistem hatası: database exploded" in t for t in texts)
    assert not any("just a warning" in t for t in texts)


async def test_commands_only_from_owner_and_never_from_bots(rig: Rig, tg) -> None:
    fake, bot, _, _ = tg
    await bot._handle_update(msg(1, "/durdur", chat_id=999))  # stranger
    await bot._handle_update(msg(2, "/durdur", is_bot=True))  # e.g. another bot
    assert rig.engine.halt_level is HaltLevel.NORMAL
    assert fake.sent == []
    await bot._handle_update(msg(3, "/durdur"))
    assert rig.engine.halt_level is HaltLevel.NO_NEW_POSITIONS
    await bot._handle_update(msg(4, "/devam"))
    assert rig.engine.halt_level is HaltLevel.NORMAL
    replies = [m["text"] for m in fake.sent if m["chat_id"] == OWNER]
    assert any("Durduruldu" in r for r in replies) and any("Kaldırıldı" in r for r in replies)


def test_command_texts(rig: Rig, tg) -> None:
    _, bot, _, _ = tg
    assert "Achilles" in bot.handle_command("/yardim")
    assert bot.handle_command("/durum") == "durum-metni"
    assert "Günlük zarar" in bot.handle_command("/limitler")
    assert bot.handle_command("/rapor hafta") == "rapor-hafta"
    assert bot.handle_command("/rapor") == "rapor-gün"
    assert "Bilinmeyen" in bot.handle_command("/sat BTC")
    assert "Onay bekleyen" in bot.handle_command("/onayla")


async def test_old_commands_are_skipped_on_startup(rig: Rig, tg) -> None:
    fake, bot, _, _ = tg
    fake.updates = [msg(10, "/durdur")]  # sent while the bot was off
    await bot._skip_backlog()
    assert bot._offset == 11
    assert rig.engine.halt_level is HaltLevel.NORMAL


async def test_chat_id_setup_helper(rig: Rig, tg) -> None:
    fake, _, api, printed = tg
    setup_bot = TelegramBot(rig.settings.telegram, api, None, rig.engine, rig.events,
                            lambda: "", lambda p: "", console=printed.append)
    await setup_bot._handle_update(msg(1, "/start", chat_id=555))
    assert "TELEGRAM_CHAT_ID=555" in printed[0]
    assert fake.sent[0]["chat_id"] == 555 and "555" in fake.sent[0]["text"]
    await setup_bot._handle_update(msg(2, "/durdur", chat_id=555))
    assert rig.engine.halt_level is HaltLevel.NORMAL  # setup mode acts on nothing


async def test_token_is_redacted_from_errors(tg) -> None:
    _, _, api, _ = tg
    bad = TelegramApi(api._url.split("/bot")[0], "999:wrong-token-value", api._session)
    with pytest.raises(TelegramError) as info:
        await bad.send_message(OWNER, "x")
    assert "wrong-token-value" not in str(info.value) and "Unauthorized" in str(info.value)


# ------------------------------------------------------------------ Jarvis

@pytest.fixture
async def jarvis(rig: Rig):
    api = JarvisApi(rig.engine, rig.events, lambda: {"mode": "PAPER_TRADING"}, READ_KEY, CONTROL_KEY)
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    yield client
    await client.close()


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def test_jarvis_needs_a_key(jarvis: TestClient) -> None:
    assert (await jarvis.get("/api/v1/status")).status == 401
    assert (await jarvis.get("/api/v1/status", headers=auth("x" * 32))).status == 401
    resp = await jarvis.get("/api/v1/status", headers=auth(READ_KEY))
    assert resp.status == 200 and (await resp.json())["mode"] == "PAPER_TRADING"


async def test_jarvis_read_key_cannot_control(rig: Rig, jarvis: TestClient) -> None:
    resp = await jarvis.post("/api/v1/control/stop", headers=auth(READ_KEY))
    assert resp.status == 403
    assert rig.engine.halt_level is HaltLevel.NORMAL


async def test_jarvis_emergency_stop_and_resume(rig: Rig, jarvis: TestClient) -> None:
    resp = await jarvis.post("/api/v1/control/stop", headers=auth(CONTROL_KEY),
                             json={"reason": "piyasada panik"})
    assert resp.status == 200 and (await resp.json())["halt_level"] == "NO_NEW_POSITIONS"
    stop_events = [e for e in rig.seen if e.kind == "manual_stop"]
    assert stop_events and "Jarvis" in stop_events[0].message and stop_events[0].should_notify
    resp = await jarvis.post("/api/v1/control/resume", headers=auth(CONTROL_KEY))
    assert (await resp.json())["halt_level"] == "NORMAL"
    assert any(e.kind == "resumed" and "Jarvis" in e.message for e in rig.seen)


async def test_jarvis_cannot_lift_owner_stop_or_limit_halts(rig: Rig, jarvis: TestClient) -> None:
    rig.engine.stop("/durdur komutu")  # the owner stopped it
    resp = await jarvis.post("/api/v1/control/resume", headers=auth(CONTROL_KEY))
    body = await resp.json()
    assert body["halt_level"] == "NO_NEW_POSITIONS" and "sahibi" in body["message"]
    rig.engine.resume()
    rig.portfolio.value = Decimal("8900")  # -11%: drawdown kill switch
    rig.engine.evaluate(T0)
    assert rig.engine.halt_level is HaltLevel.CLOSE_ALL
    await jarvis.post("/api/v1/control/resume", headers=auth(CONTROL_KEY))
    assert rig.engine.halt_level is HaltLevel.CLOSE_ALL


async def test_jarvis_has_no_order_or_limit_endpoints(jarvis: TestClient) -> None:
    for path in ("/api/v1/control/order", "/api/v1/control/limits", "/api/v1/control/approve"):
        assert (await jarvis.post(path, headers=auth(CONTROL_KEY))).status == 404


async def test_jarvis_events_and_report(rig: Rig, jarvis: TestClient) -> None:
    emit_trade_closed(rig.events, trade_id="a", symbol="BTC/TRY", quantity=Decimal("0.001"),
                      price=Decimal("3600000"), pnl_try=Decimal("100"), pnl_usd=Decimal("2.86"))
    resp = await jarvis.get("/api/v1/report?period=gün", headers=auth(READ_KEY))
    report = await resp.json()
    assert report["categories"]["Kazanç"]["pnl_try"] == "100.00"
    assert report["categories"]["Kripto birimleri"] == {"BTC": "100.00"}
    assert "+100,00 TL" in report["text"]
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    resp = await jarvis.get("/api/v1/events", params={"since": since}, headers=auth(READ_KEY))
    kinds = [e["kind"] for e in (await resp.json())["events"]]
    assert "trade_closed" in kinds


def test_jarvis_keys_must_be_long_and_different(rig: Rig) -> None:
    with pytest.raises(ValueError):
        JarvisApi(rig.engine, rig.events, dict, "short", CONTROL_KEY)
    with pytest.raises(ValueError):
        JarvisApi(rig.engine, rig.events, dict, READ_KEY, READ_KEY)


async def test_jarvis_webhook_is_signed(rig: Rig) -> None:
    received: list[tuple[bytes, str]] = []

    async def hook(request: web.Request) -> web.Response:
        received.append((await request.read(), request.headers["X-KriptoBotu-Signature"]))
        return web.Response()

    app = web.Application()
    app.router.add_post("/hook", hook)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        wh = JarvisWebhook(str(server.make_url("/hook")), READ_KEY, session, summary_only=False)
        rig.events.subscribe(wh.on_event)
        rig.events.emit(Level.KRITIK, "drawdown_halt", "acil")
        task = asyncio.create_task(wh.run())
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        task.cancel()
    await server.close()
    body, sig = received[0]
    assert json.loads(body)["kind"] == "drawdown_halt"
    assert sig == hmac.new(READ_KEY.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ misc

def test_report_text_and_periods() -> None:
    log = EventLog(None)
    empty = build_report(log.recent(), "gün", T0)
    assert "henüz kapanan işlem yok" in empty.text()
    emit_trade_closed(log, trade_id="1", symbol="ETH/TRY", quantity=Decimal("0.1"), price=Decimal("1"),
                      pnl_try=Decimal("250.5"), pnl_usd=Decimal("7"))
    emit_trade_closed(log, trade_id="2", symbol="BTC/TRY", quantity=Decimal("0.1"), price=Decimal("1"),
                      pnl_try=Decimal("-50"), pnl_usd=Decimal("-1.4"), reason="stop-loss")
    r = build_report(log.recent(), "gün", datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert (r.trades, r.wins, r.losses, r.pnl_try) == (2, 1, 1, Decimal("200.5"))
    assert "Kazanç: +200,50 TL" in r.text() and "BTC -50,00 TL" in r.text()
    # Turkey midnight 2026-10-26 = 21:00 UTC on the 25th; week starts Monday 26th.
    assert period_start(T0, "gün", 3) == datetime(2026, 10, 25, 21, tzinfo=timezone.utc)
    assert period_start(T0 + timedelta(days=3), "hafta", 3) == datetime(2026, 10, 25, 21, tzinfo=timezone.utc)
    assert period_start(T0, "ay", 3) == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)


def test_event_log_persists_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    EventLog(path).emit(Level.UYARI, "x", "mesaj", amount=Decimal("1.5"))
    again = list(EventLog(path).read_since(datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert again[0].message == "mesaj" and again[0].data["amount"] == "1.5"


def test_env_file_parsing_and_key_generation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = tmp_path / ".env"
    env.with_name(".env.txt").write_text(
        '﻿# yorum\nTELEGRAM_BOT_TOKEN="abc:def"\nTELEGRAM_CHAT_ID= 111222333\nEMPTY=\n', encoding="utf-8")
    values = load_env(env)
    assert values == {"TELEGRAM_BOT_TOKEN": "abc:def", "TELEGRAM_CHAT_ID": "111222333"}
    added = ensure_keys(env, ["JARVIS_READ_KEY", "JARVIS_CONTROL_KEY"])
    assert added == ["JARVIS_READ_KEY", "JARVIS_CONTROL_KEY"]
    values = load_env(env)
    assert values["TELEGRAM_BOT_TOKEN"] == "abc:def"  # untouched
    assert len(values["JARVIS_READ_KEY"]) >= 32 and values["JARVIS_READ_KEY"] != values["JARVIS_CONTROL_KEY"]
    assert ensure_keys(env, ["JARVIS_READ_KEY", "JARVIS_CONTROL_KEY"]) == []


def test_env_template_created_once(tmp_path: Path) -> None:
    from cryptobot.envfile import ensure_env_file

    env = tmp_path / ".env"
    assert ensure_env_file(env) is True
    assert "TELEGRAM_BOT_TOKEN=" in env.read_text(encoding="utf-8")
    env.write_text("TELEGRAM_BOT_TOKEN=keep\n", encoding="utf-8")
    assert ensure_env_file(env) is False
    assert load_env(env)["TELEGRAM_BOT_TOKEN"] == "keep"
