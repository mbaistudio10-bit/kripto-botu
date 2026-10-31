"""Bot -> Jarvis sync: overview is pushed signed; only control-signed stop/resume requests run, once."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from cryptobot.api.jarvis import JarvisSync

READ = "r" * 32
CONTROL = "c" * 32


def sign(key: str, body: bytes) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def stop(self, reason: str, by: str = "owner") -> str:
        self.calls.append(("stop", by))
        return "ok"

    def resume(self, confirm: bool = False, by: str = "owner") -> str:
        self.calls.append(("resume", by))
        return "ok"


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status, self._body, self.headers = status, body, headers

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Plays Jarvis: records what the bot sends, answers with the queued reply."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, dict[str, str]]] = []
        self.reply: tuple[int, bytes, dict[str, str]] = (200, b'{"komutlar": []}', {})

    def post(self, url: str, data: bytes, headers: dict[str, str], timeout: object) -> FakeResponse:
        self.sent.append((data, headers))
        return FakeResponse(*self.reply)


def make(session: FakeSession, engine: FakeEngine, control: str | None = CONTROL) -> JarvisSync:
    return JarvisSync("https://jarvis.example/api/kriptobot/senkron", READ, control, engine,
                      lambda: {"mode": "PAPER_TRADING", "risk": {"halt_level": "NORMAL"}}, session)


def answer(commands: list[dict], key: str | None = CONTROL) -> tuple[int, bytes, dict[str, str]]:
    body = json.dumps({"komutlar": commands}).encode()
    return 200, body, ({"X-Jarvis-Signature": sign(key, body)} if key else {})


@pytest.mark.asyncio
async def test_overview_is_sent_signed_with_read_key() -> None:
    session, engine = FakeSession(), FakeEngine()
    assert await make(session, engine).sync_once() is False
    body, headers = session.sent[0]
    assert headers["X-KriptoBotu-Signature"] == sign(READ, body)
    payload = json.loads(body)
    assert payload["mode"] == "PAPER_TRADING" and "sent_at" in payload and payload["done_commands"] == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_signed_stop_runs_once_and_is_reported_back() -> None:
    session, engine = FakeSession(), FakeEngine()
    sync = make(session, engine)
    session.reply = answer([{"id": "k1", "tur": "durdur", "neden": "test"}])
    assert await sync.sync_once() is True
    assert engine.calls == [("stop", "jarvis")]
    # Jarvis repeats the command before it saw the report: it must not run again.
    assert await sync.sync_once() is False
    assert engine.calls == [("stop", "jarvis")]
    assert json.loads(session.sent[1][0])["done_commands"] == ["k1"]


@pytest.mark.asyncio
async def test_resume_goes_through_engine_as_jarvis() -> None:
    session, engine = FakeSession(), FakeEngine()
    session.reply = answer([{"id": "k2", "tur": "devam"}])
    assert await make(session, engine).sync_once() is True
    assert engine.calls == [("resume", "jarvis")]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "x" * 32, READ])
async def test_commands_without_control_signature_are_ignored(key: str | None) -> None:
    session, engine = FakeSession(), FakeEngine()
    session.reply = answer([{"id": "k3", "tur": "durdur"}], key)
    assert await make(session, engine).sync_once() is False
    assert engine.calls == []


@pytest.mark.asyncio
async def test_no_control_key_means_no_commands() -> None:
    session, engine = FakeSession(), FakeEngine()
    session.reply = answer([{"id": "k4", "tur": "durdur"}])
    assert await make(session, engine, control=None).sync_once() is False
    assert engine.calls == []


@pytest.mark.asyncio
async def test_unknown_command_does_nothing() -> None:
    session, engine = FakeSession(), FakeEngine()
    session.reply = answer([{"id": "k5", "tur": "al", "pair": "BTC/TRY"}])
    assert await make(session, engine).sync_once() is False
    assert engine.calls == []


@pytest.mark.asyncio
async def test_jarvis_error_keeps_reports_for_next_time() -> None:
    session, engine = FakeSession(), FakeEngine()
    sync = make(session, engine)
    session.reply = answer([{"id": "k6", "tur": "durdur"}])
    await sync.sync_once()
    session.reply = (503, b"", {})
    assert await sync.sync_once() is False
    session.reply = (200, b"{}", {})
    await sync.sync_once()
    assert json.loads(session.sent[2][0])["done_commands"] == ["k6"]
