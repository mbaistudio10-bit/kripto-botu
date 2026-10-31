"""Entry point: python -m cryptobot [--config config/settings.yaml] [--once] [--backtest [--days N]]"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
import time
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

import aiohttp

from cryptobot.api.jarvis import JarvisApi, JarvisSync, JarvisWebhook, start_api
from cryptobot.api.panel import PanelApi, backtest_summary, build_overview, panel_links, start_panel
from cryptobot.autostart import autostart_command, hide_console
from cryptobot.backtest.run import run_backtest, send_to_telegram
from cryptobot.config import Settings, load_settings
from cryptobot.daily import DailySummaryTask, SummaryStore
from cryptobot.domain.models import TradingMode
from cryptobot.envfile import ensure_env_file, ensure_keys, load_env
from cryptobot.events import ErrorEventHandler, EventLog, Level
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError
from cryptobot.exchanges.registry import create_adapter
from cryptobot.health import Links, health
from cryptobot.market_data.candles import CandleService
from cryptobot.market_data.fx import FxRateService
from cryptobot.live.setup import LiveContext, prepare_live, run_selftest
from cryptobot.live.trader import LiveTrader
from cryptobot.market_data.service import MarketDataService, check_pairs
from cryptobot.notify.telegram import TelegramApi, TelegramBot, parse_chat_id
from cryptobot.report import build_report, period_start
from cryptobot.paper.portfolio import PaperPortfolio, PaperStateError
from cryptobot.paper.trader import PaperTrader
from cryptobot.risk.engine import RiskEngine
from cryptobot.status import build_snapshot, render_console, render_telegram, write_snapshot
from cryptobot.strategy.runner import StrategyRunner
from cryptobot.strategy.trend import TrendBreakoutStrategy, bar_name

log = logging.getLogger("cryptobot")

LIVE_DISCLAIMER = (
    "GERÇEK PARA MODU: bot OKX TR hesabınızda gerçek alım-satım emri gönderir. Zarar etme ihtimali vardır, "
    "kâr garantisi yoktur. Yatırım tavsiyesi değildir. Acil durdurma: Ctrl+C, Telegram /durdur veya Kontrol Merkezi."
)
DISCLAIMER = (
    "Bu yazılım yatırım tavsiyesi değildir. Geçmiş performans gelecekteki getiriyi "
    "garanti etmez. Faz 5: trend stratejisiyle sahte parayla deneme işlemleri yapar, "
    "Kontrol Merkezi'nden izlenir, borsaya hiçbir emir göndermez."
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


def setup_telegram(
    settings: Settings, events: EventLog, engine: RiskEngine, snapshot_fn, session: aiohttp.ClientSession,
    panel_text=None,
) -> TelegramBot | None:
    cfg = settings.telegram
    if not cfg.enabled:
        return None
    env = load_env(cfg.env_file)
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(f"Telegram ayarlı değil ({cfg.env_file} dosyasında TELEGRAM_BOT_TOKEN yok). "
              "Telefona bildirim gitmeyecek. Kurulum: KURULUM.md bölüm 3.")
        return None
    chat_id = parse_chat_id(env.get("TELEGRAM_CHAT_ID"))
    if chat_id is None:
        print("Telegram: TELEGRAM_CHAT_ID yok. Telegram'da kendi botunuza bir mesaj yazın; "
              "kimlik numaranızı hem burada hem Telegram'da göstereceğim.")
    else:
        print("Telegram bağlı: uyarılar telefonunuza gidecek. Komutlar için Telegram'da /yardim yazın.")

    def report_text(period: str) -> str:
        now = datetime.now(timezone.utc)
        since = period_start(now, period, settings.risk.reset_utc_offset_hours)
        return build_report(events.read_since(since), period, since).text()

    api = TelegramApi(cfg.api_base_url, token, session)
    return TelegramBot(
        cfg, api, chat_id, engine, events,
        status_text=lambda: render_telegram(snapshot_fn(), settings.risk.reset_utc_offset_hours),
        report_text=report_text,
        panel_text=panel_text,
    )


def setup_jarvis(
    settings: Settings, events: EventLog, engine: RiskEngine, snapshot_fn, session: aiohttp.ClientSession,
    overview_fn=None, summaries: SummaryStore | None = None,
) -> tuple[JarvisApi | None, JarvisWebhook | None, JarvisSync | None]:
    cfg = settings.jarvis
    if not cfg.enabled:
        return None, None, None
    env_path = settings.telegram.env_file
    try:
        added = ensure_keys(env_path, ["JARVIS_READ_KEY", "JARVIS_CONTROL_KEY"])
    except OSError as exc:
        print(f"Jarvis anahtarları {env_path} dosyasına yazılamadı: {exc}. Jarvis bağlantısı kapalı.")
        return None, None, None
    if added:
        print(f"Jarvis için iki anahtar oluşturuldu ve {env_path} dosyasına yazıldı "
              "(JARVIS_READ_KEY: sadece okuma, JARVIS_CONTROL_KEY: acil durdurma). Bunları sadece Jarvis'e verin.")
    env = load_env(env_path)
    try:
        api = JarvisApi(engine, events, snapshot_fn, env["JARVIS_READ_KEY"], env.get("JARVIS_CONTROL_KEY"),
                        settings.risk.reset_utc_offset_hours, overview=overview_fn, summaries=summaries)
    except (KeyError, ValueError) as exc:
        print(f"Jarvis bağlantısı kapalı: anahtarlar hatalı ({exc}).")
        return None, None, None
    webhook = None
    if env.get("JARVIS_WEBHOOK_URL"):
        webhook = JarvisWebhook(env["JARVIS_WEBHOOK_URL"], env["JARVIS_READ_KEY"], session,
                                summary_only=cfg.webhook_events == "summary")
        events.subscribe(webhook.on_event)
    sync = None
    if env.get("JARVIS_SYNC_URL") and overview_fn is not None:
        sync = JarvisSync(env["JARVIS_SYNC_URL"], env["JARVIS_READ_KEY"], env.get("JARVIS_CONTROL_KEY"),
                          engine, overview_fn, session)
    return api, webhook, sync


def setup_panel(settings: Settings, events: EventLog, engine: RiskEngine, overview_fn) -> PanelApi | None:
    if not settings.panel.enabled:
        return None
    env_path = settings.telegram.env_file
    try:
        ensure_keys(env_path, ["PANEL_KEY"],
                    "Control Center key, created automatically. It opens your panel; do not share it.")
        return PanelApi(engine, events, overview_fn, load_env(env_path)["PANEL_KEY"],
                        settings.risk.reset_utc_offset_hours)
    except (OSError, KeyError, ValueError) as exc:
        print(f"Kontrol Merkezi açılamadı: {exc}")
        return None


def panel_text(settings: Settings) -> str:
    key = load_env(settings.telegram.env_file).get("PANEL_KEY", "")
    lines = ["📱 Kontrol Merkezi bağlantıları (bu mesajı kimseyle paylaşmayın):"]
    for where, link in panel_links(key, settings.panel.port, settings.panel.home_network):
        lines.append(f"{where}: {link}")
    lines.append("Açtıktan sonra tarayıcı menüsünden 'Ana ekrana ekle' (telefon) veya "
                 "'Uygulama olarak yükle' (bilgisayar) seçin.")
    if settings.panel.home_network:
        lines.append("Telefon, bilgisayarla aynı ev Wi-Fi'ında olmalı.")
    return "\n".join(lines)


async def _refresh_listing(adapter: ExchangeAdapter, settings: Settings, engine: RiskEngine) -> None:
    """Re-check every few minutes whether our pairs are still trading (delist rule)."""
    while True:
        await asyncio.sleep(settings.status.listing_check_minutes * 60)
        try:
            result = await check_pairs(adapter, settings.pairs, settings.market_type)
        except ExchangeError as exc:
            log.warning("listing check failed: %s", exc)
            continue
        engine.set_live_pairs(result.available)


async def run(settings: Settings, once: bool, background: bool = False) -> int:
    started_at = datetime.now(timezone.utc)
    events = EventLog(None if once else settings.status.events_file)
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

        live = settings.trading_mode is TradingMode.LIVE_TRADING and not once
        live_ctx: LiveContext | None = None
        if live:
            live_ctx = await prepare_live(adapter, settings, pairs, settings.live.state_file.exists())
            if live_ctx is None:
                return 1  # a settings/keys problem: restarting would not help

        md = MarketDataService(adapter, pairs, settings.market_data)
        fx = FxRateService(md, settings.fx)
        paper_error = None
        if live_ctx is not None:
            capital, capital_ccy, state_file = live_ctx.start_cash, settings.paper.cash_currency, settings.live.state_file
        else:
            capital, capital_ccy = settings.starting_capital, settings.starting_capital_currency
            state_file = None if once else settings.paper.state_file
        try:
            portfolio = PaperPortfolio(capital, capital_ccy, settings.risk_currency, md, fx, state_file)
        except PaperStateError as exc:
            paper_error = exc
            portfolio = PaperPortfolio(capital or settings.starting_capital, capital_ccy,
                                       settings.risk_currency, md, fx, None)
        portfolio.paper = live_ctx is None
        engine = RiskEngine(
            settings, md, fx,
            portfolio=portfolio,
            events=events,
            state_path=None if once else settings.status.risk_state_file,
            live_pairs=pairs,
            started_at=started_at,
        )

        if paper_error is not None:
            log.error("paper account unreadable: %s", paper_error)
            engine.stop(f"Hesap dosyası ({state_file}) okunamadı")
        elif portfolio.restored:
            print(f"{'Gerçek hesap kayıtları' if live_ctx else 'Deneme hesabı'} kaldığı yerden devam ediyor: "
                  f"{len(portfolio.positions)} açık pozisyon.")
        strategy_pairs = [p for p in settings.strategy.pairs if p in pairs]
        traded = set(settings.strategy.pairs) if settings.strategy.enabled else None
        if live_ctx is not None:
            trader: PaperTrader = LiveTrader(portfolio, engine, md, events, settings.paper, settings.live,
                                             live_ctx.client, live_ctx.instruments, traded)
        else:
            trader = PaperTrader(portfolio, engine, md, events, settings.paper, traded)
        candles = CandleService(adapter, strategy_pairs, settings.strategy.bar_minutes,
                                settings.strategy.history_bars, settings.strategy.refresh_seconds,
                                None if once else settings.backtest.cache_dir)
        runner = StrategyRunner(trader, engine, candles, TrendBreakoutStrategy(settings.strategy),
                                settings.strategy, settings.exits, events, pairs)

        links = Links()
        jarvis_running = False

        def snapshot() -> dict:
            snap = build_snapshot(settings, md, fx, started_at, engine.evaluate(), portfolio.snapshot())
            if settings.strategy.enabled:
                snap["strategy"] = runner.snapshot()
            snap["health"] = health(settings, snap, links)
            return snap

        summaries = SummaryStore(settings.daily_summary.file)

        def state() -> dict:
            snap = snapshot()
            snap["daily_summary"] = summaries.get()
            snap["backtest"] = backtest_summary(settings.backtest.report_file)
            snap["jarvis"] = {
                "enabled": settings.jarvis.enabled, "running": jarvis_running, "webhook": links.jarvis_webhook,
                "url": f"http://{settings.jarvis.host}:{settings.jarvis.port}/api/v1" if jarvis_running else None,
            }
            return snap

        def overview() -> dict:
            return build_overview(engine, events, state, settings.risk.reset_utc_offset_hours)

        tasks = [asyncio.create_task(md.run(), name="market-data")]
        session = aiohttp.ClientSession()
        telegram: TelegramBot | None = None
        jarvis_runner = None
        panel_runner = None
        shutdown_note = "⚪ Kripto Botu kapatıldı. Yeni işlem açılmıyor, izleme durdu."
        try:
            if once:
                # Wait until every pair has a price (max 20s), print once, exit.
                for _ in range(40):
                    if all(md.ticker(p) and md.book(p) for p in pairs):
                        break
                    await asyncio.sleep(0.5)
                snap = snapshot()
                write_snapshot(settings.status.status_file, snap)
                print(render_console(snap))
                return 0 if all(md.ticker(p) for p in pairs) else 4

            logging.getLogger().addHandler(ErrorEventHandler(events))
            with contextlib.suppress(OSError):
                if ensure_env_file(settings.telegram.env_file):
                    print(f"Gizli ayar dosyası oluşturuldu: {settings.telegram.env_file.resolve()} "
                          "(Telegram anahtarınızı buraya yazacaksınız, KURULUM.md bölüm 3).")
            panel = setup_panel(settings, events, engine, state)
            telegram = setup_telegram(settings, events, engine, snapshot, session,
                                      (lambda: panel_text(settings)) if panel is not None else None)
            jarvis_api, jarvis_hook, jarvis_sync = setup_jarvis(settings, events, engine, snapshot, session, overview,
                                                   summaries)
            if jarvis_api is not None:
                try:
                    jarvis_runner = await start_api(jarvis_api, settings.jarvis.host, settings.jarvis.port)
                    jarvis_running = True
                    print(f"Jarvis bağlantısı açık: http://{settings.jarvis.host}:{settings.jarvis.port}/api/v1 "
                          "(durum okuma + acil durdurma, emir yok).")
                except OSError as exc:
                    print(f"Jarvis bağlantısı açılamadı (port {settings.jarvis.port} dolu olabilir): {exc}")
            env = load_env(settings.telegram.env_file)
            links = Links(telegram_token=telegram is not None,
                          telegram_chat=parse_chat_id(env.get("TELEGRAM_CHAT_ID")) is not None,
                          jarvis_api=jarvis_running,
                          jarvis_webhook=jarvis_hook is not None or jarvis_sync is not None)
            if panel is not None:
                host = "0.0.0.0" if settings.panel.home_network else "127.0.0.1"
                try:
                    panel_runner = await start_panel(panel, host, settings.panel.port)
                    print("\n" + panel_text(settings) + "\n")
                    if settings.panel.open_on_start and not background:
                        with contextlib.suppress(Exception):
                            webbrowser.open(panel_links(env.get("PANEL_KEY", ""), settings.panel.port, False)[0][1])
                except OSError as exc:
                    print(f"Kontrol Merkezi açılamadı (port {settings.panel.port} dolu olabilir): {exc}")
            if jarvis_hook is not None:
                tasks.append(asyncio.create_task(jarvis_hook.run(), name="jarvis-webhook"))
            if jarvis_sync is not None:
                tasks.append(asyncio.create_task(jarvis_sync.run(), name="jarvis-sync"))
                print("Jarvis'e otomatik bağlantı açık: durum her dakika Jarvis'e gönderiliyor (tünel gerekmez).")
            if settings.daily_summary.enabled:
                daily = DailySummaryTask(snapshot, events, summaries, settings.daily_summary.at,
                                         settings.risk.reset_utc_offset_hours, started_at)
                tasks.append(asyncio.create_task(daily.run(), name="daily-summary"))
            tasks.append(asyncio.create_task(_refresh_listing(adapter, settings, engine)))
            if settings.trading_mode in (TradingMode.PAPER_TRADING, TradingMode.LIVE_TRADING):
                tasks.append(asyncio.create_task(trader.run(), name="trader"))
                if isinstance(trader, LiveTrader):
                    tasks.append(asyncio.create_task(trader.reconcile_loop(), name="live-reconcile"))
                    print(LIVE_DISCLAIMER)
                if settings.strategy.enabled and strategy_pairs:
                    tasks.append(asyncio.create_task(candles.run(), name="candles"))
                    tasks.append(asyncio.create_task(runner.run(), name="strategy"))
                    s = settings.strategy
                    print(f"Strateji açık: trend takibi ({', '.join(map(str, strategy_pairs))}), "
                          f"{bar_name(s.bar_minutes)} mumlar, {bar_name(s.trend_bar_minutes)} trend filtresi. "
                          "Alış sadece bir mum kapanınca ve kurallar uyunca olur; saatlerce alış olmaması normaldir.")
                else:
                    print("Strateji kapalı (settings.yaml → strategy → enabled: false): yeni alış yapılmaz.")
            if settings.test_trigger is not None:
                print("Not: ayar dosyasındaki eski 'test_trigger' bölümü artık kullanılmıyor (yerine strateji geldi).")
            if telegram is not None:
                tasks.append(asyncio.create_task(telegram.run(
                    f"✅ Achilles: Kripto Botu başladı ({settings.trading_mode.value}). "
                    "Uyarılar buraya gelecek. Komutlar için /yardim yazın."
                ), name="telegram"))
            events.emit(Level.BILGI, "started", "Bot başladı.", mode=settings.trading_mode.value)
            while True:
                await asyncio.sleep(settings.status.print_every_seconds)
                for task in tasks:
                    if task.done():
                        task.result()  # surface the crash
                snap = snapshot()
                write_snapshot(settings.status.status_file, snap)
                print(render_console(snap))
        except Exception as exc:
            log.exception("bot crashed")
            shutdown_note = f"🔴 KRİTİK\nKripto Botu bir hata nedeniyle durdu: {type(exc).__name__}. Bilgisayardan yeniden başlatın."
            raise
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if telegram is not None:
                await telegram.send_now(shutdown_note)
            if jarvis_runner is not None:
                await jarvis_runner.cleanup()
            if panel_runner is not None:
                await panel_runner.cleanup()
            await session.close()


async def live_test(settings: Settings, value_try: float) -> int:
    async with create_adapter(settings.exchange) as adapter:
        return await run_selftest(adapter, settings, Decimal(str(value_try)))


async def backtest(settings: Settings, days: int | None) -> int:
    print("\nGEÇMİŞ VERİ TESTİ: strateji geçmiş fiyatlar üzerinde denenecek. Bu birkaç dakika sürebilir.")
    async with create_adapter(settings.exchange) as adapter:
        code, text = await run_backtest(settings, adapter, days)
    print("\n" + text)
    if code == 0:
        print(f"\nRapor kaydedildi: {settings.backtest.report_file}")
        if await send_to_telegram(settings, text):
            print("Rapor Telegram'a da gönderildi.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cryptobot")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--backtest", action="store_true", help="test the strategy on past prices")
    parser.add_argument("--days", type=int, default=None, help="backtest length in days")
    parser.add_argument("--live", action="store_true",
                        help="REAL MONEY: apply config/canli.yaml on top of the settings (trading_mode LIVE_TRADING)")
    parser.add_argument("--live-test", action="store_true",
                        help="real-money self-test: buy a tiny amount and sell it again (asks for EVET first)")
    parser.add_argument("--live-test-tl", type=float, default=30.0, help="size of the live self-test in TL")
    parser.add_argument("--background", action="store_true",
                        help="run hidden without a window; restart after a crash or lost internet")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="Windows: start hidden at every login (on) or stop doing so (off)")
    args = parser.parse_args(argv)
    if args.autostart:
        return autostart_command(args.autostart)
    if args.background:
        hide_console()

    overlay = Path(args.config).with_name("canli.yaml") if (args.live or args.live_test) else None
    try:
        settings = load_settings(args.config, overlay)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Ayar dosyası okunamadı veya hatalı ({args.config}):\n{exc}")
        return 1

    setup_logging(settings.status.log_file)
    print(LIVE_DISCLAIMER if settings.trading_mode is TradingMode.LIVE_TRADING else DISCLAIMER)
    log.info("starting, mode=%s exchange=%s", settings.trading_mode.value, settings.exchange.name)
    try:
        if args.backtest:
            return asyncio.run(backtest(settings, args.days))
        if args.live_test:
            return asyncio.run(live_test(settings, args.live_test_tl))
        if args.once:
            return asyncio.run(run(settings, True))
        return supervise(settings, args.background)
    except KeyboardInterrupt:
        print("\nBot durduruldu.")
        return 0


def supervise(settings: Settings, background: bool, sleep=time.sleep) -> int:
    """Keep the bot running: after a crash, or when the exchange cannot be reached
    (e.g. no internet yet right after the computer starts), wait and start again.
    Only one copy runs at a time."""
    guard = single_instance(settings.panel.port - 1)
    if guard is None:
        print("Kripto Botu zaten çalışıyor (arka planda olabilir). İkinci kopya açılmadı.")
        return 0
    with guard, keep_awake(settings.keep_awake):
        while True:
            try:
                code = asyncio.run(run(settings, False, background))
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - logged in run(); start again
                code = -1
            if code in (0, 1):  # stopped on purpose / settings error: do not loop
                return code
            wait = RESTART_WAIT_SECONDS
            print(f"Bot {wait} saniye sonra yeniden başlatılacak...")
            log.warning("bot stopped with code %s, restarting in %ss", code, wait)
            sleep(wait)


RESTART_WAIT_SECONDS = 60


def single_instance(port: int) -> socket.socket | None:
    """A local port as a lock: the second copy cannot bind it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


@contextlib.contextmanager
def keep_awake(enabled: bool):
    """On Windows, stop the computer from going to sleep while the bot runs (the screen may still turn off)."""
    set_state = None
    if enabled and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            set_state = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
            set_state(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    try:
        yield
    finally:
        if set_state is not None:
            with contextlib.suppress(Exception):
                set_state(0x80000000)


if __name__ == "__main__":
    sys.exit(main())
