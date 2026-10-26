"""OKX adapter against a local fake OKX server (REST + WebSocket), sample replies from the v5 docs."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from cryptobot.config import ExchangeConfig
from cryptobot.domain.models import MarketType, Symbol
from cryptobot.exchanges.base import ExchangeError
from cryptobot.exchanges.okx import OkxAdapter

TICKER_ROW = {
    "instType": "SPOT", "instId": "BTC-TRY", "last": "3512345.6", "lastSz": "0.001",
    "askPx": "3512400", "askSz": "0.2", "bidPx": "3512300", "bidSz": "0.5",
    "open24h": "3400000", "high24h": "3550000", "low24h": "3390000",
    "volCcy24h": "98765432.1", "vol24h": "28.1", "ts": "1761468000000",
    "sodUtc0": "3450000", "sodUtc8": "3440000",
}
BOOK_ROW = {
    "asks": [["3512400", "0.2", "0", "3"], ["3512500", "1.0", "0", "5"]],
    "bids": [["3512300", "0.5", "0", "2"], ["3512000", "2.0", "0", "4"]],
    "ts": "1761468000000",
}
INSTRUMENTS = [
    {"instId": "BTC-TRY", "state": "live", "tickSz": "0.1", "lotSz": "0.00000001", "minSz": "0.00001"},
    {"instId": "ETH-TRY", "state": "suspend", "tickSz": "0.01", "lotSz": "0.000001", "minSz": "0.0001"},
]


def ok(data: list) -> web.Response:
    return web.json_response({"code": "0", "msg": "", "data": data})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            continue
        if msg.data == "ping":
            await ws.send_str("pong")
            continue
        sub = json.loads(msg.data)
        await ws.send_str(json.dumps({"event": "subscribe", "arg": sub["args"][0], "connId": "x"}))
        for arg in sub["args"]:
            row = dict(TICKER_ROW, instId=arg["instId"])
            await ws.send_str(json.dumps({"arg": arg, "data": [row]}))
    return ws


@pytest.fixture
async def adapter():
    app = web.Application()

    async def instruments(request: web.Request) -> web.Response:
        assert request.query["instType"] == "SPOT"
        return ok(INSTRUMENTS)

    async def ticker(request: web.Request) -> web.Response:
        if request.query["instId"] == "XXX-TRY":
            return web.json_response({"code": "51001", "msg": "Instrument ID does not exist", "data": []})
        return ok([TICKER_ROW])

    async def books(request: web.Request) -> web.Response:
        assert request.query["sz"] == "20"
        return ok([BOOK_ROW])

    app.router.add_get("/api/v5/public/instruments", instruments)
    app.router.add_get("/api/v5/market/ticker", ticker)
    app.router.add_get("/api/v5/market/books", books)
    app.router.add_get("/ws", ws_handler)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    cfg = ExchangeConfig(name="okx", rest_base_url=base, ws_public_url=base.replace("http", "ws") + "/ws")
    async with OkxAdapter(cfg) as a:
        yield a
    await server.close()


async def test_fetch_instruments(adapter: OkxAdapter) -> None:
    inst = await adapter.fetch_instruments(MarketType.SPOT)
    assert [(i.symbol, i.is_live) for i in inst] == [
        (Symbol("BTC", "TRY"), True), (Symbol("ETH", "TRY"), False)
    ]
    assert inst[0].tick_size == Decimal("0.1")


async def test_fetch_ticker(adapter: OkxAdapter) -> None:
    t = await adapter.fetch_ticker(Symbol("BTC", "TRY"))
    assert t.last == Decimal("3512345.6")
    assert t.bid == Decimal(3512300) and t.ask == Decimal(3512400)
    assert t.volume_24h_quote == Decimal("98765432.1")
    assert t.exchange_ts.year == 2025


async def test_exchange_error_code_raises(adapter: OkxAdapter) -> None:
    with pytest.raises(ExchangeError, match="51001"):
        await adapter.fetch_ticker(Symbol("XXX", "TRY"))


async def test_fetch_order_book(adapter: OkxAdapter) -> None:
    book = await adapter.fetch_order_book(Symbol("BTC", "TRY"), 20)
    assert book.bids[0].price == Decimal(3512300)
    assert book.asks[1].size == Decimal("1.0")
    assert book.depth_quote("bid") == Decimal("3512300") * Decimal("0.5") + Decimal(3512000) * 2


async def test_stream_tickers(adapter: OkxAdapter) -> None:
    pairs = [Symbol("BTC", "TRY"), Symbol("USDT", "TRY")]
    got = []
    async for t in adapter.stream_tickers(pairs):
        got.append(t.symbol)
        if len(got) == 2:
            break
    assert got == pairs


async def test_has_no_order_methods(adapter: OkxAdapter) -> None:
    # Phase 1 safety: nothing that could send an order exists.
    assert not [m for m in dir(adapter) if "order" in m and "book" not in m]


def test_parse_candles_drops_unfinished_bar_and_sorts_oldest_first() -> None:
    from cryptobot.exchanges.okx import parse_candles

    rows = [
        ["1700007200000", "3", "4", "2", "3.5", "10", "35", "35", "0"],  # still forming
        ["1700003600000", "2", "3", "1", "3", "5", "15", "15", "1"],
        ["1700000000000", "1", "2", "0.5", "2", "4", "8", "8", "1"],
    ]
    out = parse_candles(rows)
    assert [c.close for c in out] == [2.0, 3.0]
    assert out[0].ts < out[1].ts
