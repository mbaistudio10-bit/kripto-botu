"""Status snapshot: printed to the console and written as JSON for the future phone view."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptobot.config import Settings
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService

_LIQUIDITY_BAND_BPS = Decimal(50)


def _num(d: Decimal | None, places: int = 2) -> str | None:
    return None if d is None else f"{d:.{places}f}"


def build_snapshot(
    settings: Settings,
    md: MarketDataService,
    fx: FxRateService,
    started_at: datetime,
    risk: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rate = fx.usd_try()
    pairs: list[dict[str, Any]] = []
    for sym in md.pairs:
        t = md.ticker(sym)
        book = md.book(sym)
        row: dict[str, Any] = {"pair": str(sym), "stale": md.is_stale(sym, now)}
        if t is not None:
            usd = fx.to_usd(t.last, sym.quote)
            row |= {
                "last": _num(t.last, 4),
                "bid": _num(t.bid, 4),
                "ask": _num(t.ask, 4),
                "last_usd": _num(usd, 4),
                "spread_bps": _num(t.spread_bps),
                "change_24h_pct": _num(t.change_24h_pct),
                "volume_24h_quote": _num(t.volume_24h_quote, 0),
                "age_seconds": round(md.age_seconds(sym, now) or 0, 1),
            }
        if book is not None:
            row |= {
                "bid_depth_50bps": _num(book.depth_quote("bid", _LIQUIDITY_BAND_BPS), 0),
                "ask_depth_50bps": _num(book.depth_quote("ask", _LIQUIDITY_BAND_BPS), 0),
                "imbalance": _num(book.imbalance, 3),
            }
        if risk is not None and str(sym) in risk["pairs"]:
            row |= risk["pairs"][str(sym)]
        pairs.append(row)
    return {
        "time": now.isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "mode": settings.trading_mode.value,
        "exchange": settings.exchange.name,
        "fx": None
        if rate is None
        else {"usd_try": _num(rate.rate, 4), "source": rate.source, "fresh": fx.is_fresh(now)},
        "new_positions_allowed": risk["new_positions_allowed"] if risk else fx.is_fresh(now),
        "risk": risk,
        "portfolio": portfolio,
        "pairs": pairs,
        "errors": md.errors,
        "last_error": md.last_error,
    }


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # atomic, so a reader never sees half a file


def render_console(snapshot: dict[str, Any]) -> str:
    """Turkish, human-friendly table for the terminal window."""
    lines = [
        "",
        f"=== Kripto Botu | {snapshot['time']} | mod: {snapshot['mode']} | borsa: {snapshot['exchange'].upper()} ===",
    ]
    fx = snapshot["fx"]
    if fx is None:
        lines.append("USD/TRY kuru: henüz yok (bekleniyor)")
    else:
        status = "güncel" if fx["fresh"] else "ESKİ - yeni pozisyon açılmaz"
        lines.append(f"USD/TRY kuru: {fx['usd_try']} ({fx['source']}, {status})")
    lines.append(f"{'Çift':<10}{'Son fiyat':>16}{'USD':>12}{'24s %':>8}{'Spread(bps)':>13}  Durum")
    for p in snapshot["pairs"]:
        if "last" not in p:
            lines.append(f"{p['pair']:<10}{'veri bekleniyor...':>16}")
            continue
        state = "ESKİ VERİ" if p["stale"] else "canlı"
        lines.append(
            f"{p['pair']:<10}{p['last']:>16}{(p['last_usd'] or '-'):>12}"
            f"{p['change_24h_pct']:>8}{p['spread_bps']:>13}  {state}"
        )
    risk = snapshot.get("risk")
    if risk is not None:
        if risk["halts"]:
            lines.append("RİSK: DURDURULDU -> " + " | ".join(h["message"] for h in risk["halts"]))
        elif risk["new_positions_allowed"]:
            lines.append("Risk: normal, yeni işlem açılabilir")
        else:
            lines.append("Risk: şu an yeni işlem açılmaz (kur eski veya sadece-izleme modu)")
        lines.append(
            f"Zarar bugün %{risk['daily_loss_pct'] or '0.00'} / %{risk['limits']['max_daily_loss_pct']}  "
            f"hafta %{risk['weekly_loss_pct'] or '0.00'} / %{risk['limits']['max_weekly_loss_pct']}  "
            f"zirveden %{risk['drawdown_pct'] or '0.00'} / %{risk['limits']['max_total_drawdown_pct']}"
        )
        for p in snapshot["pairs"]:
            if p.get("problems"):
                lines.append(f"  {p['pair']} işleme uygun değil: " + "; ".join(p["problems"]))
    pf = snapshot.get("portfolio")
    if pf is not None:
        lines.append(
            f"Deneme hesabı: {pf['equity_try']} TL (≈ {pf['equity_usd'] or '-'} USD) | nakit {pf['cash_try']} TL | "
            f"kapanan işlem {pf['trades_closed']}, gerçekleşen kâr {pf['realized_try']} TL"
        )
        for pos in pf["positions"]:
            lines.append(
                f"  Açık: {pos['pair']} {pos['quantity']} @ {pos['entry_price']} -> {pos['price']} "
                f"({pos['unrealized_try']} TL, %{pos['unrealized_pct']}) stop {pos['stop_price']}"
            )
    lines += strategy_lines(snapshot)
    if snapshot["errors"]:
        lines.append(f"Uyarı: {snapshot['errors']} bağlantı hatası oldu. Son: {snapshot['last_error']}")
    return "\n".join(lines)


def strategy_lines(snapshot: dict[str, Any]) -> list[str]:
    """One line per pair: what the strategy decided at the last closed candle."""
    st = snapshot.get("strategy")
    if not st:
        return []
    out = [f"Strateji (trend, {st['bar_minutes']} dk mum / {st['trend_bar_minutes'] // 60} sa trend):"]
    for pair, p in st["pairs"].items():
        if not p["ready"]:
            why = "geçmiş mumlar yükleniyor"
        elif p["decision"] is None:
            why = "sıradaki mum kapanışı bekleniyor"
        elif p["decision"] == "LONG":
            why = "ALIŞ sinyali: " + (p["why"] or "")
        else:
            why = "bekle, " + (p["why"] or "")
        out.append(f"  {pair}: {why}")
    return out


def _tr_num(text: str | None, places: int = 2) -> str:
    if text is None:
        return "-"
    d = Decimal(text)
    return f"{d:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_telegram(snapshot: dict[str, Any], utc_offset_hours: int = 3) -> str:
    """Short status for the /durum command."""
    risk = snapshot.get("risk") or {}
    mode = snapshot["mode"]
    mode_tr = {"PAPER_TRADING": "kağıt üzerinde deneme, gerçek para yok",
               "READ_ONLY": "sadece izleme"}.get(mode, mode)
    lines = ["📊 Kripto Botu durumu", f"Mod: {mode} ({mode_tr})"]
    if risk.get("halts"):
        lines.append("Durum: ⛔ " + " | ".join(h["message"] for h in risk["halts"]))
    elif snapshot["new_positions_allowed"]:
        lines.append("Durum: ✅ normal, yeni işlem açılabilir")
    else:
        lines.append("Durum: ⏸ şu an yeni işlem açılmaz (kur verisi eski)")
    if risk:
        lines.append(
            f"Bakiye: {_tr_num(risk.get('equity'))} TL | bugünkü zarar %{_tr_num(risk.get('daily_loss_pct'))}"
        )
        lines.append(f"Açık pozisyon: {risk['open_positions']} / {risk['limits']['max_concurrent_positions']}")
    pf = snapshot.get("portfolio")
    if pf is not None:
        lines.append(
            f"Deneme hesabı: {_tr_num(pf['equity_try'])} TL (≈ {_tr_num(pf['equity_usd'])} USD), "
            f"başlangıç {_tr_num(pf['starting_capital'])} TL"
        )
        cash = [f"{_tr_num(v)} {'TL' if k == 'TRY' else k}" for k, v in pf["balances"].items() if v != "0"]
        if cash:
            lines.append("Boştaki para: " + ", ".join(cash))
        lines.append(
            f"Kapanan işlemlerden kâr: {_tr_num(pf['realized_try'])} TL ({pf['trades_closed']} işlem), "
            f"ödenen komisyon {_tr_num(pf['fees_try'])} TL"
        )
        for pos in pf["positions"]:
            lines.append(
                f"• {pos['pair']}: alış {_tr_num(pos['entry_price'])}, şimdi {_tr_num(pos['price'])}, "
                f"{_tr_num(pos['unrealized_try'])} TL (%{_tr_num(pos['unrealized_pct'])}), "
                f"stop {_tr_num(pos['stop_price'])}"
            )
    fx = snapshot["fx"]
    if fx is None:
        lines.append("USD/TRY: henüz yok")
    else:
        lines.append(f"USD/TRY: {_tr_num(fx['usd_try'], 2)} ({'güncel' if fx['fresh'] else 'ESKİ'})")
    lines.append("Fiyatlar:")
    for p in snapshot["pairs"]:
        if "last" not in p:
            lines.append(f"• {p['pair']}: veri bekleniyor")
            continue
        mark = "✅" if p.get("tradable", True) else "⚠️ " + "; ".join(p.get("problems", []))
        lines.append(f"• {p['pair']}: {_tr_num(p['last'])} ({_tr_num(p['change_24h_pct'])}%) {mark}")
    lines += [line.strip() if line.startswith("  ") else line for line in strategy_lines(snapshot)]
    local = datetime.fromisoformat(snapshot["time"]) + timedelta(hours=utc_offset_hours)
    lines.append(f"Saat: {local:%d.%m.%Y %H:%M} (TR)")
    return "\n".join(lines)
