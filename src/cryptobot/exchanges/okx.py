"""OKX adapter using public (no API key) v5 REST and WebSocket endpoints.

Docs: https://www.okx.com/docs-v5/en/
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiohttp

from cryptobot.config import ExchangeConfig
from cryptobot.domain.models import BookLevel, Instrument, MarketType, OrderBook, Symbol, Ticker
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError

log = logging.getLogger(__name__)

_INST_TYPE = {MarketType.SPOT: "SPOT"}
# OKX drops idle sockets after 30s; we ping well before that.
_WS_PING_SECONDS = 20
_WS_MAX_BACKOFF_SECONDS = 60


def to_okx_symbol(symbol: Symbol) -> str:
    return f"{symbol.base}-{symbol.quote}"


def from_okx_symbol(inst_id: str) -> Symbol:
    return Symbol.parse(inst_id)


def _ts(ms: str | int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _dec(value: Any) -> Decimal:
    # OKX sends "" for missing numbers (e.g. no bids yet).
    return Decimal(value) if value not in (None, "") else Decimal(0)


def parse_ticker(row: dict[str, Any]) -> Ticker:
    return Ticker(
        symbol=from_okx_symbol(row["instId"]),
        last=_dec(row.get("last")),
        bid=_dec(row.get("bidPx")),
        ask=_dec(row.get("askPx")),
        bid_size=_dec(row.get("bidSz")),
        ask_size=_dec(row.get("askSz")),
        high_24h=_dec(row.get("high24h")),
        low_24h=_dec(row.get("low24h")),
        open_24h=_dec(row.get("open24h")),
        volume_24h_base=_dec(row.get("vol24h")),
        # For SPOT, volCcy24h is in the quote currency.
        volume_24h_quote=_dec(row.get("volCcy24h")),
        exchange_ts=_ts(row["ts"]),
    )


def parse_order_book(symbol: Symbol, row: dict[str, Any]) -> OrderBook:
    def levels(raw: list[list[str]]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(_dec(lv[0]), _dec(lv[1])) for lv in raw)

    return OrderBook(
        symbol=symbol,
        bids=levels(row.get("bids", [])),
        asks=levels(row.get("asks", [])),
        exchange_ts=_ts(row["ts"]),
    )


def parse_instrument(row: dict[str, Any]) -> Instrument:
    return Instrument(
        symbol=from_okx_symbol(row["instId"]),
        exchange_symbol=row["instId"],
        market_type=MarketType.SPOT,
        tick_size=_dec(row.get("tickSz")),
        lot_size=_dec(row.get("lotSz")),
        min_size=_dec(row.get("minSz")),
        is_live=row.get("state") == "live",
    )


class OkxAdapter(ExchangeAdapter):
    name = "okx"

    def __init__(self, config: ExchangeConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self._config.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise ExchangeError("adapter not connected; call connect() first")
        return self._session

    async def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        url = self._config.rest_base_url.rstrip("/") + path
        try:
            async with self.session.get(url, params=params) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise ExchangeError(f"OKX HTTP {resp.status} on {path}: {text[:200]}")
                body = json.loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            raise ExchangeError(f"OKX request {path} failed: {exc!r}") from exc
        if body.get("code") != "0":
            raise ExchangeError(f"OKX error on {path}: code={body.get('code')} msg={body.get('msg')}")
        data = body.get("data")
        if not isinstance(data, list):
            raise ExchangeError(f"OKX reply on {path} has no data list")
        return data

    async def fetch_instruments(self, market_type: MarketType) -> list[Instrument]:
        inst_type = _INST_TYPE.get(market_type)
        if inst_type is None:
            raise ExchangeError(f"market type {market_type.value} not supported yet")
        rows = await self._get("/api/v5/public/instruments", {"instType": inst_type})
        return [parse_instrument(r) for r in rows]

    async def fetch_ticker(self, symbol: Symbol) -> Ticker:
        rows = await self._get("/api/v5/market/ticker", {"instId": to_okx_symbol(symbol)})
        if not rows:
            raise ExchangeError(f"no ticker for {symbol}")
        return parse_ticker(rows[0])

    async def fetch_order_book(self, symbol: Symbol, depth: int) -> OrderBook:
        rows = await self._get(
            "/api/v5/market/books", {"instId": to_okx_symbol(symbol), "sz": str(depth)}
        )
        if not rows:
            raise ExchangeError(f"no order book for {symbol}")
        return parse_order_book(symbol, rows[0])

    async def stream_tickers(self, symbols: Sequence[Symbol]) -> AsyncIterator[Ticker]:
        sub = {
            "op": "subscribe",
            "args": [{"channel": "tickers", "instId": to_okx_symbol(s)} for s in symbols],
        }
        backoff = 1.0
        while True:
            try:
                async with self.session.ws_connect(
                    self._config.ws_public_url, heartbeat=None, autoping=True
                ) as ws:
                    await ws.send_str(json.dumps(sub))
                    log.info("OKX websocket connected, subscribed to %d pairs", len(symbols))
                    backoff = 1.0
                    async for ticker in self._read_ws(ws):
                        yield ticker
                log.warning("OKX websocket closed by server, reconnecting")
            except (aiohttp.ClientError, asyncio.TimeoutError, ExchangeError, ValueError, KeyError) as exc:
                log.warning("OKX websocket error: %r; retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_MAX_BACKOFF_SECONDS)

    async def _read_ws(self, ws: aiohttp.ClientWebSocketResponse) -> AsyncIterator[Ticker]:
        while True:
            try:
                msg = await ws.receive(timeout=_WS_PING_SECONDS)
            except asyncio.TimeoutError:
                # Quiet market: send OKX's text ping so the socket stays open.
                await ws.send_str("ping")
                continue
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                return
            if msg.type is not aiohttp.WSMsgType.TEXT or msg.data == "pong":
                continue
            body = json.loads(msg.data)
            if body.get("event") == "error":
                raise ExchangeError(f"OKX websocket error: {body.get('msg')} (code {body.get('code')})")
            if body.get("arg", {}).get("channel") != "tickers":
                continue
            for row in body.get("data", []):
                yield parse_ticker(row)
