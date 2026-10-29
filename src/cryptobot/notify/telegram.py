"""Telegram alerts and owner commands.

Safety rules:
  * Only the owner's private chat (TELEGRAM_CHAT_ID) is answered; messages from
    other chats and from bots (e.g. another assistant bot) are ignored.
  * Commands can only stop/resume NEW positions, show status and approve
    looser limits. There is no command that places or changes an order.
  * The token is never printed or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from cryptobot.config import TelegramConfig
from cryptobot.events import INSTANT_KINDS, SYSTEM_ERROR, Event, EventLog, Level
from cryptobot.risk.engine import RiskEngine

log = logging.getLogger(__name__)

_PREFIX = {Level.KRITIK: "🔴 KRİTİK", Level.UYARI: "🟠 UYARI", Level.BILGI: "🟢 BİLGİ"}

HELP_TEXT = (
    "Achilles - Kripto Botu komutları:\n"
    "/durum - botun anlık durumu\n"
    "/limitler - risk limitleri ve ne kadarının kullanıldığı\n"
    "/rapor - bugünkü kazanç özeti (/rapor hafta, /rapor ay)\n"
    "/durdur - yeni işlem açmayı durdur (bot izlemeye devam eder)\n"
    "/devam - durdurmayı kaldır\n"
    "/onayla - ayar dosyasındaki gevşetilmiş limitleri onayla\n"
    "/panel - Kontrol Merkezi bağlantısı (bilgisayar ve telefon)\n"
    "Alım, satım, stop-loss ve sistem hatalarında size anında mesaj gelir.\n"
    "Bu bot Telegram'dan emir göndermez. Yatırım tavsiyesi değildir."
)


class TelegramError(Exception):
    pass


class TelegramApi:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession) -> None:
        self._url = f"{base_url.rstrip('/')}/bot{token}/"
        self._token = token
        self._session = session

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    async def call(self, method: str, http_timeout: float = 15, **params: Any) -> Any:
        try:
            async with self._session.post(
                self._url + method, json=params, timeout=aiohttp.ClientTimeout(total=http_timeout)
            ) as resp:
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise TelegramError(self._redact(f"{type(exc).__name__}: {exc}")) from None
        if not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description") if isinstance(body, dict) else body
            retry = (body.get("parameters") or {}).get("retry_after") if isinstance(body, dict) else None
            err = TelegramError(self._redact(f"{method} failed: {desc}"))
            err.retry_after = retry  # type: ignore[attr-defined]
            raise err
        return body["result"]

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", http_timeout=timeout + 10, **params)


class TelegramBot:
    def __init__(
        self,
        config: TelegramConfig,
        api: TelegramApi,
        chat_id: int | None,
        engine: RiskEngine,
        events: EventLog,
        status_text: Callable[[], str],
        report_text: Callable[[str], str],
        console: Callable[[str], None] = print,
        panel_text: Callable[[], str] | None = None,
    ) -> None:
        self._panel_text = panel_text
        self._config = config
        self._api = api
        self._chat_id = chat_id
        self._engine = engine
        self._events = events
        self._status_text = status_text
        self._report_text = report_text
        self._console = console
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._last_sent: dict[str, datetime] = {}
        self._offset: int | None = None
        self._seen_strangers: set[int] = set()
        self._token_warned = False
        events.subscribe(self.on_event)

    # ---------------------------------------------------------------- alerts

    def on_event(self, event: Event) -> None:
        if not event.should_notify or self._chat_id is None:
            return
        if event.level is not Level.KRITIK and event.kind not in INSTANT_KINDS:
            last = self._last_sent.get(event.key)
            minutes = (self._config.error_cooldown_minutes if event.kind == SYSTEM_ERROR
                       else self._config.alert_cooldown_minutes)
            cooldown = timedelta(minutes=minutes)
            if last is not None and event.time - last < cooldown:
                return
        self._last_sent[event.key] = event.time
        self.enqueue(f"{_PREFIX[event.level]}\n{event.message}")

    def enqueue(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.error("telegram queue full, dropping message")

    async def _sender(self) -> None:
        while True:
            text = await self._queue.get()
            for attempt in range(5):
                try:
                    await self._api.send_message(self._chat_id, text)  # type: ignore[arg-type]
                    break
                except TelegramError as exc:
                    log.warning("telegram send failed (try %d): %s", attempt + 1, exc)
                    wait = getattr(exc, "retry_after", None) or min(2 ** attempt, 30)
                    await asyncio.sleep(wait)

    # ---------------------------------------------------------------- commands

    def handle_command(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return HELP_TEXT
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("/start", "/yardim", "/yardım", "/help"):
            return HELP_TEXT
        if cmd == "/durum":
            return self._status_text()
        if cmd == "/limitler":
            return self._engine.limits_text()
        if cmd == "/durdur":
            return self._engine.stop("/durdur komutu")
        if cmd == "/devam":
            return self._engine.resume(confirm=arg.upper() == "ONAY")
        if cmd == "/onayla":
            return self._engine.approve_limits()
        if cmd == "/panel":
            return self._panel_text() if self._panel_text else "Kontrol Merkezi ayarlarda kapalı."
        if cmd == "/rapor":
            period = {"hafta": "hafta", "ay": "ay"}.get(arg.lower(), "gün")
            return self._report_text(period)
        return "Bilinmeyen komut. Komut listesi için /yardim yazın."

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        text = msg.get("text") or ""
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type") != "private" or sender.get("is_bot"):
            return
        if self._chat_id is None:
            # Setup helper: tell whoever writes to the bot their chat id, act on nothing.
            if chat_id not in self._seen_strangers:
                self._seen_strangers.add(chat_id)
                name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
                self._console(
                    f"\nTelegram'dan mesaj geldi: {name} (@{sender.get('username', '-')}), "
                    f"chat id = {chat_id}\nBu sizseniz .env dosyasına TELEGRAM_CHAT_ID={chat_id} "
                    "yazıp kaydedin ve botu yeniden başlatın."
                )
            await self._safe_reply(
                chat_id,
                f"Merhaba! Sizin Telegram kimlik numaranız (chat id): {chat_id}\n"
                f".env dosyasına TELEGRAM_CHAT_ID={chat_id} yazıp botu yeniden başlatın.",
            )
            return
        if chat_id != self._chat_id:
            if chat_id not in self._seen_strangers:
                self._seen_strangers.add(chat_id)
                log.warning("ignored telegram message from unknown chat %s", chat_id)
            return
        if not text.startswith("/"):
            await self._safe_reply(chat_id, "Komut listesi için /yardim yazın.")
            return
        try:
            reply = self.handle_command(text)
        except Exception:  # noqa: BLE001 - a bad command must never crash the bot
            log.exception("telegram command failed: %s", text)
            reply = "Komut çalıştırılırken bir hata oldu. Ayrıntı logs/bot.log dosyasında."
        await self._safe_reply(chat_id, reply)

    async def _safe_reply(self, chat_id: int, text: str) -> None:
        try:
            await self._api.send_message(chat_id, text)
        except TelegramError as exc:
            log.warning("telegram reply failed: %s", exc)

    async def _poller(self) -> None:
        while True:
            try:
                updates = await self._api.get_updates(self._offset, self._config.poll_timeout_seconds)
            except TelegramError as exc:
                log.warning("telegram getUpdates failed: %s", exc)
                if not self._token_warned and ("Unauthorized" in str(exc) or "Not Found" in str(exc)):
                    self._token_warned = True
                    self._console("Telegram: bot anahtarı (TELEGRAM_BOT_TOKEN) hatalı görünüyor. "
                                  ".env dosyasını kontrol edin. Bot Telegram olmadan çalışmaya devam ediyor.")
                await asyncio.sleep(5)
                continue
            for update in updates:
                self._offset = int(update["update_id"]) + 1
                await self._handle_update(update)

    async def _skip_backlog(self) -> None:
        """Ignore commands sent while the bot was off (an old /durdur must not fire now)."""
        with contextlib.suppress(TelegramError):
            pending = await self._api.get_updates(None, 0)
            if pending:
                self._offset = int(pending[-1]["update_id"]) + 1

    async def run(self, startup_message: str | None = None) -> None:
        await self._skip_backlog()
        if startup_message and self._chat_id is not None:
            self.enqueue(startup_message)
        await asyncio.gather(self._sender(), self._poller())

    async def send_now(self, text: str, timeout: float = 5) -> None:
        """Best-effort direct send, used for the shutdown notice."""
        if self._chat_id is None:
            return
        with contextlib.suppress(TelegramError, asyncio.TimeoutError):
            await asyncio.wait_for(self._api.send_message(self._chat_id, text), timeout)


def parse_chat_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None
