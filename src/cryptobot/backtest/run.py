"""`KriptoBotu --backtest`: download history, test the strategy, print a Turkish report."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from cryptobot.backtest.engine import Backtester
from cryptobot.backtest.report import DISCLAIMER, render
from cryptobot.config import Settings
from cryptobot.domain.models import Candle, Symbol
from cryptobot.envfile import load_env
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError
from cryptobot.market_data.candles import load_history
from cryptobot.notify.telegram import TelegramApi, TelegramError, parse_chat_id
from cryptobot.strategy.trend import TrendBreakoutStrategy

log = logging.getLogger(__name__)


def _tr(x: float, places: int = 0) -> str:
    return f"{x:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def split_point(start: datetime, end: datetime, in_sample_pct: float, bar: timedelta) -> datetime:
    """Chronological split, on a bar boundary: everything before is in-sample."""
    raw = start + (end - start) * (in_sample_pct / 100)
    return start + ((raw - start) // bar) * bar


async def run_backtest(settings: Settings, adapter: ExchangeAdapter, days: int | None = None,
                       now: datetime | None = None) -> tuple[int, str]:
    cfg = settings.backtest
    days = days or cfg.days
    strategy = TrendBreakoutStrategy(settings.strategy)
    bar, minutes = strategy.bar, settings.strategy.bar_minutes
    now = now or datetime.now(timezone.utc)
    want_start = now - timedelta(days=days)
    fetch_from = want_start - strategy.warmup_bars * bar

    series: dict[Symbol, list[Candle]] = {}
    try:
        for sym in settings.strategy.pairs:
            series[sym] = await load_history(adapter, sym, minutes, fetch_from, cfg.cache_dir, now,
                                             progress=print)
            print(f"  {sym}: {len(series[sym])} mum")
        fx: list[Candle] | None = None
        if settings.fx.source_pair not in series:
            fx = await load_history(adapter, settings.fx.source_pair, minutes, fetch_from,
                                    cfg.cache_dir, now, progress=print)
        else:
            fx = series[settings.fx.source_pair]
    except ExchangeError as exc:
        msg = f"Geçmiş fiyatlar indirilemedi: {exc}\nİnternet bağlantınızı kontrol edip tekrar deneyin."
        return 2, msg

    usable = {s: c for s, c in series.items() if len(c) > strategy.warmup_bars + 50}
    if not usable:
        return 3, "Test için yeterli geçmiş veri yok (çiftler yeni listelenmiş olabilir)."
    # Trading starts only once every pair has its warm-up behind it.
    start = max(want_start, max(c[strategy.warmup_bars].ts for c in usable.values()))
    end = max(c[-1].ts for c in usable.values()) + bar
    split = split_point(start, end, cfg.in_sample_pct, bar)

    bt = Backtester(strategy, settings.exits, settings.risk, settings.paper, cfg.slippage_bps,
                    float(settings.starting_capital))
    pct = cfg.in_sample_pct
    is_ = bt.run(usable, start, split, f"Öğrenme dönemi (ilk %{pct:g})", fx)
    oos = bt.run(usable, split, end, f"Sınama dönemi (son %{100 - pct:g}, kural bu dönemi hiç görmedi)", fx)

    s = settings.strategy
    header = [
        f"Çiftler: {', '.join(str(p) for p in usable)} | {s.bar_minutes} dakikalık mumlar, "
        f"trend filtresi {s.trend_bar_minutes // 60} saatlik",
        f"Başlangıç parası: {_tr(float(settings.starting_capital))} TL (sahte) | komisyon "
        f"%{_tr(float(settings.paper.fee_bps) / 100, 2)} + kayma %{_tr(cfg.slippage_bps / 100, 2)} (her alış ve satışta)",
        f"Kurallar: risk/işlem %{settings.risk.max_risk_per_trade_pct}, en fazla "
        f"{settings.risk.max_concurrent_positions} pozisyon, günlük %{settings.risk.max_daily_loss_pct} / "
        f"haftalık %{settings.risk.max_weekly_loss_pct} / zirveden %{settings.risk.max_total_drawdown_pct} sınırı",
        f"Her dönem kendi başına {_tr(float(settings.starting_capital))} TL ile başlar.",
    ]
    if len(usable) < len(series):
        missing = [str(sym) for sym in series if sym not in usable]
        header.append(f"Yeterli verisi olmadığı için atlanan: {', '.join(missing)}")
    text = render(is_, oos, header)
    try:
        cfg.report_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.report_file.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write backtest report: %s", exc)
    return 0, text


async def send_to_telegram(settings: Settings, text: str) -> bool:
    env = load_env(settings.telegram.env_file)
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), parse_chat_id(env.get("TELEGRAM_CHAT_ID"))
    if not settings.telegram.enabled or not token or chat_id is None:
        return False
    async with aiohttp.ClientSession() as session:
        api = TelegramApi(settings.telegram.api_base_url, token, session)
        with contextlib.suppress(TelegramError):
            await api.send_message(chat_id, text)
            return True
    return False


__all__ = ["DISCLAIMER", "run_backtest", "send_to_telegram", "split_point"]
