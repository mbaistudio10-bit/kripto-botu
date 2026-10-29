"""Signed (private) OKX v5 requests for live trading: balances, orders, key permissions.

Works the same on OKX TR (https://tr.okx.com) and global OKX; only the base URL differs,
and an API key only works on the site it was created on.

The calls are blocking on purpose (standard library only). A live order takes a second or
two, and the bot checks stops every 2 seconds, so a short pause is harmless and keeps the
order logic simple and easy to check.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any
from urllib.parse import urlencode

from cryptobot.exchanges.base import ExchangeError

log = logging.getLogger(__name__)

# (method, url, headers, body, timeout seconds) -> (http status, text)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, str]]

# Names the keys are read from in the .env file. The first one of each row is the documented name.
KEY_NAMES = ("OKX_API_KEY", "OKX_KEY", "OKX_APIKEY")
SECRET_NAMES = ("OKX_API_SECRET", "OKX_SECRET_KEY", "OKX_SECRET", "OKX_API_SECRET_KEY")
PASSPHRASE_NAMES = ("OKX_API_PASSPHRASE", "OKX_PASSPHRASE", "OKX_API_PASSWORD", "OKX_API_PASS")

_FINISHED = {"filled", "canceled", "mmp_canceled"}


@dataclass(frozen=True)
class OkxCredentials:
    key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:  # never print secrets, not even by accident in a log
        return "OkxCredentials(key=***, secret=***, passphrase=***)"


def load_credentials(env: Mapping[str, str]) -> tuple[OkxCredentials | None, list[str]]:
    """Read the three values from the .env mapping. Returns (credentials, names still missing)."""
    def first(names: tuple[str, ...]) -> str | None:
        return next((env[n] for n in names if env.get(n)), None)

    key, secret, passphrase = first(KEY_NAMES), first(SECRET_NAMES), first(PASSPHRASE_NAMES)
    missing = [names[0] for names, v in ((KEY_NAMES, key), (SECRET_NAMES, secret),
                                         (PASSPHRASE_NAMES, passphrase)) if not v]
    if missing:
        return None, missing
    assert key and secret and passphrase
    return OkxCredentials(key, secret, passphrase), []


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of `step` (lot size)."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round up to a multiple of `step` (price tick)."""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def plain(value: Decimal) -> str:
    """Decimal as a plain string without exponent: 1E-8 -> '0.00000001'."""
    return f"{value:f}"


@dataclass(frozen=True, slots=True)
class Balance:
    available: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    state: str  # live, partially_filled, filled, canceled, ...
    filled: Decimal  # base units filled (gross, before a fee taken in the base currency)
    avg_price: Decimal
    fee: Decimal  # positive cost
    fee_ccy: str

    @property
    def finished(self) -> bool:
        return self.state in _FINISHED


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                      timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URL from settings
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExchangeError(f"connection failed: {exc!r}") from exc


class OkxPrivateClient:
    def __init__(self, base_url: str, credentials: OkxCredentials, timeout: float = 10,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._base = base_url.rstrip("/")
        self._cred = credentials
        self._timeout = timeout
        self._transport = transport or _urllib_transport
        self._sleep = sleep

    # ------------------------------------------------------------ plumbing

    def sign(self, timestamp: str, method: str, path: str, body: str) -> str:
        msg = f"{timestamp}{method}{path}{body}".encode()
        return base64.b64encode(hmac.new(self._cred.secret.encode(), msg, hashlib.sha256).digest()).decode()

    def _request(self, method: str, path: str, params: dict[str, str] | None = None,
                 payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        request_path = path + (("?" + urlencode(params)) if params else "")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        headers = {
            "OK-ACCESS-KEY": self._cred.key,
            "OK-ACCESS-SIGN": self.sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._cred.passphrase,
            "Content-Type": "application/json",
        }
        status, text = self._transport(method, self._base + request_path, headers,
                                       body.encode() if body else None, self._timeout)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise ExchangeError(f"OKX HTTP {status} on {path}: {text[:200]}") from None
        code = parsed.get("code")
        if status != 200 or code != "0":
            detail = parsed.get("msg") or text[:200]
            data = parsed.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("sMsg"):
                detail = f"{detail} / {data[0]['sMsg']}"
            raise ExchangeError(f"OKX error on {path}: HTTP {status} code={code} {detail}")
        data = parsed.get("data")
        if not isinstance(data, list):
            raise ExchangeError(f"OKX reply on {path} has no data list")
        return data

    # ------------------------------------------------------------ account

    def account_config(self) -> dict[str, Any]:
        rows = self._request("GET", "/api/v5/account/config")
        if not rows:
            raise ExchangeError("OKX returned no account configuration")
        return rows[0]

    def key_permissions(self) -> set[str]:
        """e.g. {'read_only', 'trade'}; 'withdraw' present means the key can move money out."""
        perm = str(self.account_config().get("perm", ""))
        return {p.strip().lower() for p in perm.split(",") if p.strip()}

    def balances(self) -> dict[str, Balance]:
        rows = self._request("GET", "/api/v5/account/balance")
        out: dict[str, Balance] = {}
        for acct in rows:
            for d in acct.get("details", []):
                total = _dec(d.get("cashBal") or d.get("eq"))
                avail = _dec(d.get("availBal")) if d.get("availBal") not in (None, "") else total
                if total or avail:
                    out[str(d["ccy"]).upper()] = Balance(avail, total)
        return out

    # ------------------------------------------------------------ orders

    def place_order(self, inst_id: str, side: str, ord_type: str, size: Decimal,
                    price: Decimal | None = None, client_id: str | None = None) -> str:
        """Spot order, size in BASE currency. ord_type: 'market' or 'ioc' (needs a limit price)."""
        payload: dict[str, Any] = {"instId": inst_id, "tdMode": "cash", "side": side,
                                   "ordType": ord_type, "sz": plain(size), "tgtCcy": "base_ccy"}
        if ord_type == "ioc":
            if price is None:
                raise ValueError("ioc order needs a price")
            payload["px"] = plain(price)
            payload.pop("tgtCcy")  # limit orders are always in base units
        if client_id:
            payload["clOrdId"] = client_id
        rows = self._request("POST", "/api/v5/trade/order", payload=payload)
        row = rows[0] if rows else {}
        if row.get("sCode") not in (None, "0"):
            raise ExchangeError(f"OKX refused the order: {row.get('sCode')} {row.get('sMsg')}")
        order_id = row.get("ordId")
        if not order_id:
            raise ExchangeError("OKX accepted the order but sent no order id")
        return str(order_id)

    def get_order(self, inst_id: str, order_id: str | None = None, client_id: str | None = None) -> OrderResult | None:
        params = {"instId": inst_id}
        if order_id:
            params["ordId"] = order_id
        elif client_id:
            params["clOrdId"] = client_id
        else:
            raise ValueError("order_id or client_id needed")
        try:
            rows = self._request("GET", "/api/v5/trade/order", params)
        except ExchangeError as exc:
            if "51603" in str(exc):  # order does not exist
                return None
            raise
        if not rows:
            return None
        r = rows[0]
        return OrderResult(
            order_id=str(r.get("ordId", order_id or "")), state=str(r.get("state", "")),
            filled=_dec(r.get("accFillSz")), avg_price=_dec(r.get("avgPx")),
            fee=abs(_dec(r.get("fee"))), fee_ccy=str(r.get("feeCcy", "")).upper(),
        )

    def cancel_order(self, inst_id: str, order_id: str) -> None:
        self._request("POST", "/api/v5/trade/cancel-order", payload={"instId": inst_id, "ordId": order_id})

    def wait_order(self, inst_id: str, order_id: str, seconds: float) -> OrderResult:
        """Poll until the order is finished; cancel it if it is still open after `seconds`."""
        waited = 0.0
        while True:
            res = self.get_order(inst_id, order_id=order_id)
            if res is None:
                raise ExchangeError(f"order {order_id} not found")
            if res.finished:
                return res
            if waited >= seconds:
                try:
                    self.cancel_order(inst_id, order_id)
                except ExchangeError as exc:
                    log.warning("cancel of %s failed: %s", order_id, exc)
                self._sleep(0.5)
                final = self.get_order(inst_id, order_id=order_id)
                return final if final is not None else res
            self._sleep(0.4)
            waited += 0.4
