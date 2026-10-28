"""Control Center: the owner's one-screen view of the whole bot, for the computer and the phone.

Served by the bot itself (default port 8770). Open it in a browser and add it to the
home screen / install it as an app (Edge or Chrome: "Uygulama olarak yükle").

  GET  /                         the page (HTML, works offline as an app shell)
  GET  /manifest.webmanifest     app name and icon for "add to home screen"
  GET  /sw.js  /icon.svg
  GET  /panel/api/state          everything in one JSON (also used by Jarvis)
  POST /panel/api/stop           {"reason": "..."}  no new positions
  POST /panel/api/resume         {"confirm": true} also lifts a drawdown halt

Every /panel/api call needs the panel key (PANEL_KEY in .env), sent as
"Authorization: Bearer <key>". The page asks for it once and remembers it.
The panel can stop and resume like the owner's Telegram commands. It cannot place
orders or change limits. Too many wrong keys from one address lock it out for a while.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from cryptobot.api.panel_page import ICON_SVG, PAGE_HTML, SERVICE_WORKER
from cryptobot.events import EventLog
from cryptobot.report import build_report, period_start
from cryptobot.risk.engine import RiskEngine

log = logging.getLogger(__name__)

PANEL_API = "/panel/api"
MIN_KEY_LENGTH = 16
MAX_FAILURES = 10           # wrong keys from one address...
LOCKOUT_SECONDS = 15 * 60   # ...lock it out this long

MANIFEST = {
    "name": "Kripto Botu Kontrol Merkezi",
    "short_name": "Kripto Botu",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f1419",
    "theme_color": "#0f1419",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
}


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str),
                             headers={"Cache-Control": "no-store"})


def backtest_summary(path: Path) -> str | None:
    """The short comparison part of the last backtest report (before the details)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text.split("\n\n\nAYRINTILAR", 1)[0].strip() or None


def lan_address() -> str | None:
    """This computer's address on the home network (no packet is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    return None


def build_overview(
    engine: RiskEngine, events: EventLog, state: Callable[[], dict[str, Any]], utc_offset_hours: int,
) -> dict[str, Any]:
    """Status snapshot plus reports, limits and the latest events: the whole picture in one dict."""
    now = datetime.now(timezone.utc)
    reports = {}
    for period in ("gün", "hafta", "ay"):
        since = period_start(now, period, utc_offset_hours)
        reports[period] = build_report(events.read_since(since), period, since).to_json()
    out = state()
    out["reports"] = reports
    out["limits_text"] = engine.limits_text()
    out["events"] = [e.to_json() for e in events.recent(50)][::-1]
    return out


def panel_links(key: str, port: int, home_network: bool) -> list[tuple[str, str]]:
    """(where, link) pairs. The key goes after '#', so it never reaches a server log."""
    links = [("Bu bilgisayarda", f"http://localhost:{port}/#k={key}")]
    ip = lan_address() if home_network else None
    if ip:
        links.append(("Telefonda (aynı ev Wi-Fi'ı)", f"http://{ip}:{port}/#k={key}"))
    return links


class PanelApi:
    def __init__(
        self,
        engine: RiskEngine,
        events: EventLog,
        state: Callable[[], dict[str, Any]],
        key: str,
        utc_offset_hours: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(key) < MIN_KEY_LENGTH:
            raise ValueError("PANEL_KEY must be at least 16 characters")
        self._engine = engine
        self._events = events
        self._state = state
        self._key = key
        self._offset = utc_offset_hours
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------- auth

    def _locked(self, remote: str) -> bool:
        now = self._clock()
        recent = [t for t in self._failures.get(remote, []) if now - t < LOCKOUT_SECONDS]
        self._failures[remote] = recent
        return len(recent) >= MAX_FAILURES

    @web.middleware
    async def _auth(self, request: web.Request, handler: Callable) -> web.StreamResponse:
        if not request.path.startswith(PANEL_API):
            return await handler(request)
        remote = request.remote or "?"
        if self._locked(remote):
            return _json({"error": "locked", "message": "Çok fazla yanlış anahtar. 15 dakika sonra deneyin."}, 429)
        header = request.headers.get("Authorization", "")
        given = header[7:].strip() if header.startswith("Bearer ") else ""
        if not given or not hmac.compare_digest(given, self._key):
            self._failures.setdefault(remote, []).append(self._clock())
            log.warning("panel: rejected request without valid key from %s", remote)
            return _json({"error": "unauthorized", "message": "Anahtar yanlış."}, 401)
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=16 * 1024)
        app.add_routes([
            web.get("/", self.page),
            web.get("/manifest.webmanifest", self.manifest),
            web.get("/sw.js", self.service_worker),
            web.get("/icon.svg", self.icon),
            web.get(f"{PANEL_API}/state", self.state),
            web.post(f"{PANEL_API}/stop", self.stop),
            web.post(f"{PANEL_API}/resume", self.resume),
        ])
        return app

    # ------------------------------------------------------------- shell

    async def page(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY",
                                     "Referrer-Policy": "no-referrer"})

    async def manifest(self, request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(MANIFEST, ensure_ascii=False),
                            content_type="application/manifest+json", charset="utf-8")

    async def service_worker(self, request: web.Request) -> web.Response:
        return web.Response(text=SERVICE_WORKER, content_type="application/javascript", charset="utf-8",
                            headers={"Cache-Control": "no-cache"})

    async def icon(self, request: web.Request) -> web.Response:
        return web.Response(text=ICON_SVG, content_type="image/svg+xml", charset="utf-8")

    # ------------------------------------------------------------- data

    async def state(self, request: web.Request) -> web.Response:
        return _json(self.full_state())

    def full_state(self) -> dict[str, Any]:
        return build_overview(self._engine, self._events, self._state, self._offset)

    async def stop(self, request: web.Request) -> web.Response:
        body = await _body(request)
        reason = str(body.get("reason") or "Kontrol Merkezi'nden durduruldu")[:200]
        message = self._engine.stop(reason, by="panel")
        return _json({"ok": True, "message": message, "halt_level": self._engine.halt_level.name})

    async def resume(self, request: web.Request) -> web.Response:
        body = await _body(request)
        message = self._engine.resume(confirm=body.get("confirm") is True, by="panel")
        return _json({"ok": True, "message": message, "halt_level": self._engine.halt_level.name})


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    with contextlib.suppress(ValueError):
        data = await request.json()
        if isinstance(data, dict):
            return data
    return {}


async def start_panel(api: PanelApi, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(api.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
