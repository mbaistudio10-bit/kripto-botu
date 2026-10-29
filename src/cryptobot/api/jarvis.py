"""Two-way link for the owner's Jarvis assistant.

Jarvis -> bot: a small HTTP API on this computer (default 127.0.0.1:8765).
  Read key (JARVIS_READ_KEY) or control key:
    GET  /api/v1/health   /api/v1/status   /api/v1/limits
    GET  /api/v1/events?since=<ISO time>&limit=100
    GET  /api/v1/report?period=gün|hafta|ay      (summary for the "X report")
    GET  /api/v1/overview   everything in one call: status, health score, reports,
                            limits, recent events, last backtest (same as the panel)
    GET  /api/v1/daily-summary?date=YYYY-MM-DD   end-of-day summary (default: latest)
  Control key only (JARVIS_CONTROL_KEY):
    POST /api/v1/control/stop    {"reason": "..."}   emergency: no new positions
    POST /api/v1/control/resume                      lifts only a stop Jarvis made
Bot -> Jarvis: if JARVIS_WEBHOOK_URL is set, the end-of-day summary (or, with
  jarvis.webhook_events: all, every notified event) is POSTed there as JSON, signed with
  HMAC-SHA256 (header X-KriptoBotu-Signature, key = read key). A summary is retried until
  Jarvis answers, so it arrives even if Jarvis was off. The bot never waits for Jarvis.

Jarvis can never place orders, change limits, or lift a loss-limit / drawdown halt.
Every control call is written to the event log and echoed to Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from aiohttp import web

from cryptobot.daily import DAILY_SUMMARY, SummaryStore
from cryptobot.events import Event, EventLog
from cryptobot.report import build_report, period_start
from cryptobot.risk.engine import RiskEngine

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
MIN_KEY_LENGTH = 16
_READ, _CONTROL = "read", "control"


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str))


class JarvisApi:
    def __init__(
        self,
        engine: RiskEngine,
        events: EventLog,
        snapshot: Callable[[], dict[str, Any]],
        read_key: str,
        control_key: str | None,
        utc_offset_hours: int = 3,
        overview: Callable[[], dict[str, Any]] | None = None,
        summaries: SummaryStore | None = None,
    ) -> None:
        self._overview = overview
        self._summaries = summaries
        if len(read_key) < MIN_KEY_LENGTH or (control_key is not None and len(control_key) < MIN_KEY_LENGTH):
            raise ValueError("Jarvis keys must be at least 16 characters")
        if control_key is not None and hmac.compare_digest(read_key, control_key):
            raise ValueError("JARVIS_READ_KEY and JARVIS_CONTROL_KEY must differ")
        self._engine = engine
        self._events = events
        self._snapshot = snapshot
        self._read_key = read_key
        self._control_key = control_key
        self._offset = utc_offset_hours

    def _role(self, request: web.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        given = header[7:].strip()
        if self._control_key and hmac.compare_digest(given, self._control_key):
            return _CONTROL
        if hmac.compare_digest(given, self._read_key):
            return _READ
        return None

    @web.middleware
    async def _auth(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        role = self._role(request)
        if role is None:
            log.warning("jarvis api: rejected request without valid key from %s", request.remote)
            return _json({"error": "unauthorized"}, 401)
        if request.path.startswith(f"{API_PREFIX}/control/") and role != _CONTROL:
            return _json({"error": "control key required"}, 403)
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=16 * 1024)
        app.add_routes([
            web.get(f"{API_PREFIX}/health", self.health),
            web.get(f"{API_PREFIX}/status", self.status),
            web.get(f"{API_PREFIX}/limits", self.limits),
            web.get(f"{API_PREFIX}/events", self.events),
            web.get(f"{API_PREFIX}/report", self.report),
            web.get(f"{API_PREFIX}/overview", self.overview),
            web.get(f"{API_PREFIX}/daily-summary", self.daily_summary),
            web.post(f"{API_PREFIX}/control/stop", self.stop),
            web.post(f"{API_PREFIX}/control/resume", self.resume),
        ])
        return app

    # ------------------------------------------------------------- read

    async def health(self, request: web.Request) -> web.Response:
        return _json({"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "halt_level": self._engine.halt_level.name})

    async def status(self, request: web.Request) -> web.Response:
        return _json(self._snapshot())

    async def limits(self, request: web.Request) -> web.Response:
        snap = self._engine.snapshot()
        return _json({"limits": snap["limits"], "halts": snap["halts"], "text": self._engine.limits_text()})

    async def events(self, request: web.Request) -> web.Response:
        try:
            since = datetime.fromisoformat(request.query["since"]) if "since" in request.query \
                else datetime.now(timezone.utc) - timedelta(days=1)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            limit = min(int(request.query.get("limit", 100)), 1000)
        except ValueError:
            return _json({"error": "bad since/limit"}, 400)
        found = list(self._events.read_since(since))[-limit:]
        return _json({"events": [e.to_json() for e in found]})

    async def overview(self, request: web.Request) -> web.Response:
        if self._overview is None:
            return _json({"error": "overview not available"}, 404)
        return _json(self._overview())

    async def daily_summary(self, request: web.Request) -> web.Response:
        found = self._summaries.get(request.query.get("date")) if self._summaries else None
        if found is None:
            return _json({"error": "no summary yet"}, 404)
        return _json(found)

    async def report(self, request: web.Request) -> web.Response:
        period = request.query.get("period", "gün")
        if period not in ("gün", "gun", "hafta", "ay"):
            return _json({"error": "period must be gün, hafta or ay"}, 400)
        period = "gün" if period == "gun" else period
        since = period_start(datetime.now(timezone.utc), period, self._offset)
        return _json(build_report(self._events.read_since(since), period, since).to_json())

    # ------------------------------------------------------------- control

    async def stop(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        reason = str(body.get("reason") or "acil durum")[:200]
        message = self._engine.stop(reason, by="jarvis")
        return _json({"ok": True, "message": message, "halt_level": self._engine.halt_level.name})

    async def resume(self, request: web.Request) -> web.Response:
        message = self._engine.resume(by="jarvis")
        return _json({"ok": True, "message": message, "halt_level": self._engine.halt_level.name})

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        with contextlib.suppress(ValueError):
            data = await request.json()
            if isinstance(data, dict):
                return data
        return {}


class JarvisWebhook:
    """Pushes notified events to Jarvis (bot -> Jarvis direction)."""

    def __init__(self, url: str, secret: str, session: aiohttp.ClientSession,
                 summary_only: bool = True) -> None:
        self._summary_only = summary_only
        self._url = url
        self._secret = secret.encode()
        self._session = session
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)

    def on_event(self, event: Event) -> None:
        if self._summary_only and event.kind != DAILY_SUMMARY:
            return
        if event.should_notify:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    async def run(self, max_wait: float = 1800) -> None:
        while True:
            event = await self._queue.get()
            body = json.dumps(event.to_json(), ensure_ascii=False, default=str).encode()
            headers = {"Content-Type": "application/json", "X-KriptoBotu-Signature": self.sign(body)}
            # A summary waits for Jarvis (up to a day); other events get 3 quick tries.
            tries = 60 if event.kind == DAILY_SUMMARY else 3
            for attempt in range(tries):
                try:
                    async with self._session.post(
                        self._url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status < 500:
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("jarvis webhook failed (try %d): %s", attempt + 1, exc)
                await asyncio.sleep(min(2 ** attempt, max_wait))


async def start_api(api: JarvisApi, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(api.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
