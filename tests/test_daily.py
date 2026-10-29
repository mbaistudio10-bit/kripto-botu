"""End-of-day summary for Telegram and Jarvis."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cryptobot.api.jarvis import JarvisApi, JarvisWebhook
from cryptobot.daily import DAILY_SUMMARY, DailySummaryTask, SummaryStore, build_summary, summary_window
from cryptobot.events import EventLog, emit_trade_closed
from tests.test_notify import CONTROL_KEY, READ_KEY, auth

SNAP = {
    "portfolio": {"equity_try": "10240.00", "equity_usd": "250.10", "total_change_try": "240.00",
                  "positions": [{"pair": "BTC/USDT", "unrealized_try": "52.10", "unrealized_pct": "1.05"}]},
    "risk": {"halts": [], "capital_guard": {"floor": "9900.00", "headroom": "340.00"}},
    "health": {"score": 9.6, "summary": "Tüm parçalar hedefte."},
}


def _task(tmp_path: Path, events: EventLog) -> DailySummaryTask:
    return DailySummaryTask(lambda: SNAP, events, SummaryStore(tmp_path / "s.jsonl"), time(22, 0), 3,
                            datetime.now(timezone.utc))


def test_summary_text_has_everything_jarvis_needs(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "e.jsonl")
    emit_trade_closed(events, trade_id="a", symbol="ETH/USDT", quantity=Decimal("0.5"), price=Decimal("3900"),
                      pnl_try=Decimal("184.2"), pnl_usd=Decimal("4.5"))
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    s = build_summary(today, SNAP, events, 3)
    text = s["text"]
    assert "gün sonu özeti" in text and "+184,20 TL" in text and "ETH" in text
    assert "10.240,00 TL" in text and "BTC/USDT" in text and "Ana para tabanı: 9.900,00 TL" in text
    assert "9,6 / 10" in text and "yatırım tavsiyesi değildir" in text
    assert s["report"]["categories"]["Al-sat işlemleri"]["total"] == 1
    # Another day has no trades.
    assert build_summary(today - timedelta(days=1), SNAP, events, 3)["report"]["categories"][
        "Al-sat işlemleri"]["total"] == 0


def test_summary_is_stored_announced_and_runs_at_the_set_time(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "e.jsonl")
    seen = []
    events.subscribe(seen.append)
    task = _task(tmp_path, events)
    task.write(date(2026, 10, 28))
    assert seen[-1].kind == DAILY_SUMMARY and seen[-1].should_notify
    assert task._store.get()["date"] == "2026-10-28"
    # 18:00 UTC = 21:00 Turkey: next run is 22:00 Turkey = 19:00 UTC the same day.
    now = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    assert task.next_run(now) == datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)
    assert task.next_run(now + timedelta(hours=1)) == datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)


def test_summary_covers_the_24_hours_up_to_the_summary_time(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "e.jsonl")
    day = date(2026, 10, 28)
    start, end = summary_window(day, time(22, 0), 3)
    # 27.10 22:00 Turkey -> 28.10 22:00 Turkey: a late-evening trade lands in the next summary.
    assert start == datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)
    late = {"time": (end + timedelta(minutes=30)).isoformat(), "level": "BILGI", "kind": "trade_closed",
            "message": "x", "data": {"symbol": "ETH/USDT", "pnl_try": "10", "pnl_usd": "0.3"}}
    (tmp_path / "e.jsonl").write_text(json.dumps(late) + "\n", encoding="utf-8")
    trades = lambda d: build_summary(d, SNAP, events, 3, time(22, 0))["report"]["categories"][  # noqa: E731
        "Al-sat işlemleri"]["total"]
    assert trades(day) == 0 and trades(day + timedelta(days=1)) == 1


def test_missed_summary_is_written_at_next_start(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "e.jsonl")
    now = datetime.now(timezone.utc)
    task = _task(tmp_path, events)
    assert task.catch_up(now) is None  # the bot did not run yesterday
    yesterday = {"time": (now - timedelta(days=1)).isoformat(), "level": "BILGI", "kind": "started",
                 "message": "Bot başladı.", "data": {}}
    (tmp_path / "e.jsonl").write_text(json.dumps(yesterday) + "\n", encoding="utf-8")
    written = task.catch_up(now)
    due = task.next_run(now) - timedelta(days=1)
    assert written is not None and written["date"] == (due + timedelta(hours=3)).date().isoformat()
    assert task.catch_up(now) is None  # only once


async def test_jarvis_gets_only_the_summary_and_can_fetch_it_later(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "e.jsonl")
    received: list[dict] = []

    async def hook(request: web.Request) -> web.Response:
        received.append(json.loads(await request.read()))
        return web.Response()

    app = web.Application()
    app.router.add_post("/hook", hook)
    server = TestServer(app)
    await server.start_server()
    store = SummaryStore(tmp_path / "s.jsonl")
    async with aiohttp.ClientSession() as session:
        wh = JarvisWebhook(str(server.make_url("/hook")), READ_KEY, session)  # summary only by default
        events.subscribe(wh.on_event)
        emit_trade_closed(events, trade_id="a", symbol="BTC/USDT", quantity=Decimal("0.001"),
                          price=Decimal("100000"), pnl_try=Decimal("10"), pnl_usd=Decimal("0.3"))
        DailySummaryTask(lambda: SNAP, events, store, time(22, 0), 3, datetime.now(timezone.utc)).write(
            date(2026, 10, 28))
        run = asyncio.create_task(wh.run())
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        run.cancel()
    await server.close()
    assert [r["kind"] for r in received] == [DAILY_SUMMARY]

    from tests.test_risk_engine import Rig  # noqa: F401 - engine not needed for reads
    api = JarvisApi(None, events, dict, READ_KEY, CONTROL_KEY, summaries=store)  # type: ignore[arg-type]
    client = TestClient(TestServer(api.app()))
    await client.start_server()
    try:
        body = await (await client.get("/api/v1/daily-summary", headers=auth(READ_KEY))).json()
        assert body["date"] == "2026-10-28" and "gün sonu özeti" in body["text"]
        missing = await client.get("/api/v1/daily-summary?date=2020-01-01", headers=auth(READ_KEY))
        assert missing.status == 404
    finally:
        await client.close()


# ----------------------------------------------------------------- running in the background

def test_autostart_script_handles_turkish_folder_names(tmp_path: Path) -> None:
    from cryptobot.autostart import set_autostart

    exe = Path("C:/Users/Ayşe/Masaüstü/KriptoBotu/KriptoBotu.exe")
    script = set_autostart(True, exe, tmp_path)
    raw = script.read_bytes()
    assert raw.startswith(b"\xff\xfe")  # UTF-16 with BOM
    text = raw.decode("utf-16")
    assert "Masaüstü" in text and "--background" in text and ", 0, False" in text
    set_autostart(False, exe, tmp_path)
    assert not script.exists()


def test_only_one_copy_runs(settings) -> None:
    from cryptobot.__main__ import single_instance

    first = single_instance(0)  # any free port
    assert first is not None
    port = first.getsockname()[1]
    try:
        assert single_instance(port) is None
    finally:
        first.close()


def test_bot_restarts_after_a_crash_or_lost_internet(settings, monkeypatch) -> None:
    import cryptobot.__main__ as m

    codes = iter([2, RuntimeError("boom"), 0])
    waits: list[float] = []

    async def fake_run(s, once, background=False):
        c = next(codes)
        if isinstance(c, Exception):
            raise c
        return c

    monkeypatch.setattr(m, "run", fake_run)
    monkeypatch.setattr(m, "single_instance", lambda port: __import__("contextlib").nullcontext())
    assert m.supervise(settings, background=True, sleep=waits.append) == 0
    assert waits == [m.RESTART_WAIT_SECONDS, m.RESTART_WAIT_SECONDS]
