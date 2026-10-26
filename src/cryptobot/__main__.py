"""Entry point: python -m cryptobot [--config config/settings.yaml] [--once]"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

from cryptobot.config import Settings, load_settings
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError
from cryptobot.exchanges.registry import create_adapter
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService, check_pairs
from cryptobot.status import build_snapshot, render_console, write_snapshot

log = logging.getLogger("cryptobot")

DISCLAIMER = (
    "Bu yazılım yatırım tavsiyesi değildir. Geçmiş performans gelecekteki getiriyi "
    "garanti etmez. Faz 1: sadece fiyat okur, hiçbir emir göndermez."
)


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    # Console gets the Turkish status table; technical details go to the log file.
    logging.basicConfig(level=logging.INFO, handlers=[file_handler])


async def startup_check(adapter: ExchangeAdapter, settings: Settings) -> list:
    print("OKX'e bağlanılıyor ve çiftler kontrol ediliyor...")
    result = await check_pairs(adapter, settings.pairs, settings.market_type)
    for p in result.available:
        print(f"  [OK]   {p} borsada var ve işlem görüyor")
    for p in result.suspended:
        print(f"  [DUR]  {p} borsada var ama şu an işleme kapalı, atlanıyor")
        log.warning("pair %s is listed but not live", p)
    for p in result.missing:
        print(f"  [YOK]  {p} bu borsa adresinde bulunamadı, atlanıyor")
        log.warning("pair %s not found on %s", p, settings.exchange.rest_base_url)
    if settings.fx.source_pair not in result.available:
        print(f"  UYARI: kur için gereken {settings.fx.source_pair} yok; USD karşılıkları hesaplanamaz.")
    return result.available


async def run(settings: Settings, once: bool) -> int:
    started_at = datetime.now(timezone.utc)
    async with create_adapter(settings.exchange) as adapter:
        try:
            pairs = await startup_check(adapter, settings)
        except ExchangeError as exc:
            print(f"\nOKX'e bağlanılamadı: {exc}\nİnternet bağlantınızı kontrol edip tekrar deneyin.")
            log.error("startup failed: %s", exc)
            return 2
        if not pairs:
            print("\nAyar dosyasındaki çiftlerin hiçbiri bulunamadı. Bot durduruluyor.")
            return 3

        md = MarketDataService(adapter, pairs, settings.market_data)
        fx = FxRateService(md, settings.fx)
        md_task = asyncio.create_task(md.run(), name="market-data")
        try:
            if once:
                # Wait until every pair has a price (max 20s), print once, exit.
                for _ in range(40):
                    if all(md.ticker(p) for p in pairs):
                        break
                    await asyncio.sleep(0.5)
                snap = build_snapshot(settings, md, fx, started_at)
                write_snapshot(settings.status.status_file, snap)
                print(render_console(snap))
                return 0 if all(md.ticker(p) for p in pairs) else 4
            while True:
                await asyncio.sleep(settings.status.print_every_seconds)
                if md_task.done():
                    md_task.result()  # surface the crash
                snap = build_snapshot(settings, md, fx, started_at)
                write_snapshot(settings.status.status_file, snap)
                print(render_console(snap))
        finally:
            md_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await md_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cryptobot")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Ayar dosyası okunamadı veya hatalı ({args.config}):\n{exc}")
        return 1

    setup_logging(settings.status.log_file)
    print(DISCLAIMER)
    log.info("starting, mode=%s exchange=%s", settings.trading_mode.value, settings.exchange.name)
    try:
        return asyncio.run(run(settings, args.once))
    except KeyboardInterrupt:
        print("\nBot durduruldu.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
