"""Start-up checks for live trading, and the one-off live self-test.

Nothing here sends an order except `run_selftest`, which asks for a typed "EVET" first.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from cryptobot.config import Settings
from cryptobot.domain.models import Instrument, Symbol
from cryptobot.envfile import load_env
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError
from cryptobot.exchanges.okx_private import (
    Balance, OkxPrivateClient, ceil_to_step, floor_to_step, load_credentials,
)
from cryptobot.fmt import fmt_num

log = logging.getLogger(__name__)

PERMISSION_HELP = (
    "OKX TR'de API anahtarı oluştururken sadece 'Okuma' ve 'Al-Sat' izni verin; 'Çekim' izni KAPALI olmalı. "
    "Yeni bir anahtar oluşturup .env dosyasındaki eskisinin yerine yazın."
)


@dataclass
class LiveContext:
    client: OkxPrivateClient
    instruments: dict[Symbol, Instrument]
    balances: dict[str, Balance]
    start_cash: Decimal  # what the bot's books start with (cash currency), 0 if it resumes old books


def say(text: str) -> None:
    print(text)


def make_client(settings: Settings) -> OkxPrivateClient | None:
    env = load_env(settings.telegram.env_file)
    creds, missing = load_credentials(env)
    if creds is None:
        say("CANLI MOD AÇILMADI: .env dosyasında şu satırlar eksik: " + ", ".join(missing))
        say("  Örnek (eşittirin sağına kendi değerinizi yazın, tırnak koymayın):")
        say("  OKX_API_KEY=...\n  OKX_API_SECRET=...\n  OKX_API_PASSPHRASE=...")
        return None
    return OkxPrivateClient(settings.exchange.rest_base_url, creds, settings.exchange.request_timeout_seconds)


def cash_cap(settings: Settings, usdt_try: Decimal | None) -> Decimal | None:
    """The bot's money limit (TL) expressed in the cash currency."""
    cap_try = settings.live.max_capital_try
    cash = settings.paper.cash_currency
    if cash == settings.risk_currency:
        return cap_try
    if cash == "USDT" and usdt_try:
        return cap_try / usdt_try
    return None


async def prepare_live(
    adapter: ExchangeAdapter, settings: Settings, pairs: list[Symbol], restoring: bool,
) -> LiveContext | None:
    """Every check that must pass before the first real order. Prints plain-language problems."""
    say("\n=== CANLI MOD (GERÇEK PARA) kontrolleri ===")
    if not settings.live.confirmed:
        say("CANLI MOD AÇILMADI: settings dosyasında 'live: confirmed: true' yazmıyor. Gerçek parayla işlem "
            "yapmak istediğinizden eminseniz bu satırı kendiniz 'true' yapın.")
        return None
    client = make_client(settings)
    if client is None:
        return None
    try:
        perms = await asyncio.to_thread(client.key_permissions)
        balances = await asyncio.to_thread(client.balances)
        instruments = {i.symbol: i for i in await adapter.fetch_instruments(settings.market_type)}
    except ExchangeError as exc:
        say(f"CANLI MOD AÇILMADI: borsa hesabına bağlanılamadı: {exc}")
        say("  Anahtarların OKX TR'den alındığını, doğru yazıldığını ve IP kısıtı varsa bu bilgisayarın "
            "adresinin tanımlı olduğunu kontrol edin. (Adres: " + settings.exchange.rest_base_url + ")")
        return None
    if "withdraw" in perms:
        say("CANLI MOD AÇILMADI: API anahtarının PARA ÇEKME izni açık. Güvenlik için bu anahtarla çalışmam. " + PERMISSION_HELP)
        return None
    if "trade" not in perms:
        say("CANLI MOD AÇILMADI: API anahtarında 'Al-Sat' izni yok. " + PERMISSION_HELP)
        return None
    say("  [OK]  API anahtarı çalışıyor (izinler: " + ", ".join(sorted(perms)) + "; para çekme izni yok)")

    missing = [str(p) for p in settings.strategy.pairs if p not in pairs]
    if missing:
        say("CANLI MOD AÇILMADI: stratejinin işlem yapacağı çiftler bu borsada yok: " + ", ".join(missing))
        wanted = {p.base for p in settings.strategy.pairs}
        found = sorted(str(s) for s in instruments if s.base in wanted and s.quote in ("USDT", "USDC", "TRY"))
        say("  OKX TR'de bulunan benzer çiftler: " + (", ".join(found) or "yok"))
        say("  Ayar dosyasında 'pairs', 'strategy: pairs' ve 'paper: cash_currency' bu çiftlere göre değişmeli.")
        return None
    cash = settings.paper.cash_currency
    have = balances.get(cash)
    say("  Hesapta: " + (", ".join(f"{fmt_num(b.available)} {c}" for c, b in sorted(balances.items())
                                  if b.total > 0) or "bakiye yok"))
    start_cash = Decimal(0)
    if not restoring:
        usdt_try = None
        if cash != settings.risk_currency:
            try:
                usdt_try = (await adapter.fetch_ticker(settings.fx.source_pair)).mid
            except ExchangeError as exc:
                say(f"CANLI MOD AÇILMADI: dolar kuru alınamadı: {exc}")
                return None
        cap = cash_cap(settings, usdt_try)
        if cap is None:
            say("CANLI MOD AÇILMADI: nakit para birimi için TL karşılığı hesaplanamıyor.")
            return None
        start_cash = min(have.available if have else Decimal(0), cap)
        if start_cash <= 0:
            say(f"CANLI MOD AÇILMADI: hesapta kullanılabilir {cash} yok. OKX TR uygulamasında 'Dönüştür' "
                f"bölümünden TL'nizi {cash}'ye çevirin (en fazla {fmt_num(settings.live.max_capital_try)} TL "
                "kadarını bot kullanır), sonra botu yeniden başlatın.")
            return None
        say(f"  Botun kullanacağı para: {fmt_num(start_cash)} {cash} "
            f"(üst sınır {fmt_num(settings.live.max_capital_try)} TL; hesabınızdaki geri kalan paraya dokunmaz)")
    return LiveContext(client, instruments, balances, start_cash)


# --------------------------------------------------------------------------- self-test

async def run_selftest(adapter: ExchangeAdapter, settings: Settings, value_try: Decimal,
                       ask=input) -> int:
    """Buy a tiny amount of the first strategy pair and sell it again, to prove keys, orders and fees work."""
    from cryptobot.market_data.service import check_pairs

    say("\n=== CANLI TEST: çok küçük gerçek alış + hemen satış ===")
    result = await check_pairs(adapter, settings.pairs, settings.market_type)
    ctx = await prepare_live(adapter, settings, result.available, restoring=True)
    if ctx is None:
        return 2
    symbol = settings.strategy.pairs[0]
    inst = ctx.instruments[symbol]
    ticker = await adapter.fetch_ticker(symbol)
    rate = Decimal(1)
    if symbol.quote != settings.risk_currency:
        rate = (await adapter.fetch_ticker(settings.fx.source_pair)).mid
    qty = max(floor_to_step(value_try / rate / ticker.ask, inst.lot_size), inst.min_size)
    cost = qty * ticker.ask
    say(f"Plan: {symbol} için ≈ {fmt_num(cost)} {symbol.quote} (≈ {fmt_num(cost * rate)} TL) tutarında GERÇEK alış, "
        "ardından hemen satış. Komisyon + fark nedeniyle küçük bir kayıp (birkaç kuruş) normaldir.")
    if ask("Onaylıyorsanız EVET yazıp Enter'a basın: ").strip().upper() != "EVET":
        say("Onaylanmadı, hiçbir emir gönderilmedi.")
        return 1
    client = ctx.client
    limit = ceil_to_step(ticker.ask * (1 + settings.live.buy_slippage_bps / Decimal(10_000)), inst.tick_size)
    try:
        oid = await asyncio.to_thread(client.place_order, inst.exchange_symbol, "buy", "ioc", qty, limit, "kbtestbuy")
        buy = await asyncio.to_thread(client.wait_order, inst.exchange_symbol, oid, settings.live.order_wait_seconds)
        say(f"Alış: {buy.filled.normalize():f} {symbol.base} @ {fmt_num(buy.avg_price)} (komisyon {buy.fee.normalize():f} {buy.fee_ccy})")
        if buy.filled <= 0:
            say("Alış dolmadı; para harcanmadı.")
            return 3
        held = buy.filled - (buy.fee if buy.fee_ccy == symbol.base else Decimal(0))
        sell_qty = floor_to_step(held, inst.lot_size)
        oid = await asyncio.to_thread(client.place_order, inst.exchange_symbol, "sell", "market", sell_qty, None, "kbtestsell")
        sell = await asyncio.to_thread(client.wait_order, inst.exchange_symbol, oid, settings.live.order_wait_seconds)
    except ExchangeError as exc:
        say(f"TEST BAŞARISIZ: {exc}")
        say("Borsa uygulamanızdan açık emir ve bakiye olup olmadığını kontrol edin.")
        return 4
    spent = buy.filled * buy.avg_price + (buy.fee if buy.fee_ccy == symbol.quote else 0)
    got = sell.filled * sell.avg_price - (sell.fee if sell.fee_ccy == symbol.quote else 0)
    say(f"Satış: {sell.filled.normalize():f} {symbol.base} @ {fmt_num(sell.avg_price)}")
    say(f"Sonuç: harcanan {fmt_num(spent)} {symbol.quote}, geri gelen {fmt_num(got)} {symbol.quote}, "
        f"fark {fmt_num(got - spent)} {symbol.quote} ({fmt_num((got - spent) * rate)} TL). TEST TAMAM: anahtarlar ve emirler çalışıyor.")
    left = held - sell.filled
    if left > 0:
        say(f"Not: {left.normalize():f} {symbol.base} çok küçük bir kalıntı hesapta kaldı (satılamayacak kadar küçük).")
    return 0
