"""`KriptoBotu --backtest`: download history, test the strategy, print a Turkish report."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from cryptobot.backtest.engine import Backtester
from cryptobot.backtest.report import DISCLAIMER, render_comparison, render_details
from cryptobot.backtest.variants import variants
from cryptobot.config import Settings
from cryptobot.domain.models import Candle, Symbol
from cryptobot.envfile import load_env
from cryptobot.exchanges.base import ExchangeAdapter, ExchangeError
from cryptobot.market_data.candles import load_history
from cryptobot.notify.telegram import TelegramApi, TelegramError, parse_chat_id
from cryptobot.strategy.indicators import resample
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
    """Returns (exit code, short report). The report file also gets the details."""
    cfg = settings.backtest
    days = days or cfg.days
    rule_sets = variants(settings.strategy, settings.exits, cfg.compare_variants)
    strategies = [TrendBreakoutStrategy(v.strategy) for v in rule_sets]
    # Download the smallest candle once; bigger candles are built from it.
    base = min(v.strategy.bar_minutes for v in rule_sets)
    base_bar = timedelta(minutes=base)
    warmup = max(st.warmup_bars * st.bar for st in strategies)
    now = now or datetime.now(timezone.utc)
    want_start = now - timedelta(days=days)
    fetch_from = want_start - warmup

    series: dict[Symbol, list[Candle]] = {}
    try:
        for sym in settings.strategy.pairs:
            series[sym] = await load_history(adapter, sym, base, fetch_from, cfg.cache_dir, now,
                                             progress=print)
            print(f"  {sym}: {len(series[sym])} mum")
        if settings.fx.source_pair in series:
            fx = series[settings.fx.source_pair]
        else:
            fx = await load_history(adapter, settings.fx.source_pair, base, fetch_from,
                                    cfg.cache_dir, now, progress=print)
    except ExchangeError as exc:
        msg = f"Geçmiş fiyatlar indirilemedi: {exc}\nİnternet bağlantınızı kontrol edip tekrar deneyin."
        return 2, msg

    usable = {s: c for s, c in series.items() if c and c[0].ts + warmup + timedelta(days=30) < now}
    if not usable:
        return 3, "Test için yeterli geçmiş veri yok (çiftler yeni listelenmiş olabilir)."
    # Every rule set trades the same dates: after the longest warm-up of all of them.
    start = max(want_start, max(c[0].ts for c in usable.values()) + warmup)
    end = max(c[-1].ts for c in usable.values()) + base_bar
    split = split_point(start, end, cfg.in_sample_pct, timedelta(days=1))
    pct = cfg.in_sample_pct

    results = []
    for v, strat in zip(rule_sets, strategies):
        data = {s: c if v.strategy.bar_minutes == base else resample(c, base, v.strategy.bar_minutes)[0]
                for s, c in usable.items()}
        bt = Backtester(strat, v.exits, settings.risk, settings.paper, cfg.slippage_bps,
                        float(settings.starting_capital), cfg.drawdown_pause_days)
        is_ = bt.run(data, start, split, f"Öğrenme dönemi (ilk %{pct:g})", fx)
        oos = bt.run(data, split, end, f"Sınama dönemi (son %{100 - pct:g}, kural bu dönemi hiç görmedi)", fx)
        results.append((v, is_, oos))

    pause = (f"acil durdurmadan sonra {cfg.drawdown_pause_days:g} gün ara verip devam eder"
             if cfg.drawdown_pause_days > 0 else "acil durdurmada durur")
    header = [
        f"Çiftler: {', '.join(str(p) for p in usable)} | başlangıç parası "
        f"{_tr(float(settings.starting_capital))} TL (sahte), her dönem yeniden",
        f"Maliyet: komisyon %{_tr(float(settings.paper.fee_bps) / 100, 2)} + kayma "
        f"%{_tr(cfg.slippage_bps / 100, 2)} (her alış ve satışta)",
        f"Risk: işlem başına %{settings.risk.max_risk_per_trade_pct}, en fazla "
        f"{settings.risk.max_concurrent_positions} pozisyon, günlük %{settings.risk.max_daily_loss_pct} / "
        f"haftalık %{settings.risk.max_weekly_loss_pct} / zirveden %{settings.risk.max_total_drawdown_pct}; "
        f"test {pause}",
    ]
    if len(usable) < len(series):
        missing = [str(sym) for sym in series if sym not in usable]
        header.append(f"Yeterli verisi olmadığı için atlanan: {', '.join(missing)}")
    summary = render_comparison(results, header, fx)
    details = "\n\n".join(f"===== {v.label} =====\n" + render_details(is_, oos) for v, is_, oos in results)
    try:
        cfg.report_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.report_file.write_text(summary + "\n\n\nAYRINTILAR\n\n" + details + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write backtest report: %s", exc)
    return 0, summary


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
