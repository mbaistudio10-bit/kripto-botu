from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptobot.config import Settings
from cryptobot.domain.models import MarketType, Symbol
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService, check_pairs
from cryptobot.status import build_snapshot, render_console, write_snapshot
from tests.conftest import FakeAdapter, make_ticker

BTC, ETH, USDT = Symbol("BTC", "TRY"), Symbol("ETH", "TRY"), Symbol("USDT", "TRY")


async def test_check_pairs_splits_available_missing_suspended() -> None:
    adapter = FakeAdapter({"BTC/TRY": "1"}, listed={"BTC/TRY": True, "ETH/TRY": False})
    result = await check_pairs(adapter, [BTC, ETH, USDT], MarketType.SPOT)
    assert result.available == [BTC]
    assert result.suspended == [ETH]
    assert result.missing == [USDT]


def test_ticker_math() -> None:
    t = make_ticker("BTC/TRY", "100", bid="99", ask="101")
    assert t.mid == Decimal(100)
    assert t.spread_bps == Decimal(200)


async def test_rest_mode_collects_prices_and_books(settings: Settings) -> None:
    adapter = FakeAdapter({"BTC/TRY": "3500000", "ETH/TRY": "130000", "USDT/TRY": "35"})
    cfg = settings.market_data.model_copy(update={"mode": "rest"})
    md = MarketDataService(adapter, [BTC, ETH, USDT], cfg)
    task = asyncio.create_task(md.run())
    await asyncio.sleep(0.05)
    task.cancel()
    assert md.ticker(BTC).last == Decimal(3500000)
    assert md.book(ETH).bids[0].price == Decimal(129999)
    assert not md.is_stale(USDT)


def test_staleness_and_fx_freshness(settings: Settings) -> None:
    md = MarketDataService(FakeAdapter({}), [BTC, USDT], settings.market_data)
    fx = FxRateService(md, settings.fx)
    assert md.is_stale(BTC) and not fx.is_fresh()  # nothing received yet

    md.on_ticker(make_ticker("USDT/TRY", "35"))
    assert fx.is_fresh()
    assert fx.to_usd(Decimal(70), "TRY") == Decimal(2)
    later = datetime.now(timezone.utc) + timedelta(seconds=301)
    assert not fx.is_fresh(later)  # 5-minute rule: block new positions
    assert md.is_stale(USDT, later)


def test_ignores_unsubscribed_pairs(settings: Settings) -> None:
    md = MarketDataService(FakeAdapter({}), [BTC], settings.market_data)
    md.on_ticker(make_ticker("SOL/TRY", "1"))
    assert md.ticker(Symbol("SOL", "TRY")) is None


async def test_snapshot_and_console(settings: Settings, tmp_path) -> None:
    adapter = FakeAdapter({"BTC/TRY": "3500000", "USDT/TRY": "35"})
    md = MarketDataService(adapter, [BTC, ETH, USDT], settings.market_data)
    for s in (BTC, USDT):
        md.on_ticker(await adapter.fetch_ticker(s))
    fx = FxRateService(md, settings.fx)
    snap = build_snapshot(settings, md, fx, datetime.now(timezone.utc))
    btc = snap["pairs"][0]
    assert btc["last_usd"] == "100000.0000"
    assert snap["new_positions_allowed"] is True
    assert snap["pairs"][1] == {"pair": "ETH/TRY", "stale": True}

    path = tmp_path / "status.json"
    write_snapshot(path, snap)
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "PAPER_TRADING"
    text = render_console(snap)
    assert "BTC/TRY" in text and "veri bekleniyor" in text
