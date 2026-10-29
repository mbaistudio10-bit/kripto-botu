"""System health and protection score: each part of the bot gets 0-10, shown on the panel.

This measures whether the bot is running safely and has what it needs (fresh data,
working limits, notifications). It says nothing about future profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptobot.config import Settings
from cryptobot.domain.models import MarketType, TradingMode

TARGET = 8.0  # every part should reach this (owner's target: 8-8.5 out of 10)

# The owner's limits from the original spec; a looser setting lowers the score.
_SPEC_LIMITS = {
    "max_daily_loss_pct": Decimal(3),
    "max_weekly_loss_pct": Decimal(6),
    "max_total_drawdown_pct": Decimal(10),
    "max_risk_per_trade_pct": Decimal(1),
    "max_concurrent_positions": Decimal(2),
}


@dataclass(frozen=True, slots=True)
class Links:
    """Which outside links are set up (filled in by __main__)."""

    telegram_token: bool = False
    telegram_chat: bool = False
    jarvis_api: bool = False
    jarvis_webhook: bool = False


@dataclass(frozen=True, slots=True)
class Part:
    key: str
    name: str
    score: float
    note: str

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "score": round(self.score, 1), "note": self.note,
                "ok": self.score >= TARGET}


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _market(snap: dict[str, Any]) -> Part:
    pairs = snap.get("pairs") or []
    if not pairs:
        return Part("market", "Piyasa verisi", 0, "Hiç çift yok.")
    live = [p for p in pairs if "last" in p and not p["stale"]]
    score = 10 * len(live) / len(pairs)
    if snap.get("errors"):
        score -= 1  # reconnects happened; data still flows if the pairs are live
    dead = [p["pair"] for p in pairs if p not in live]
    note = "Tüm fiyatlar canlı." if not dead else "Canlı değil: " + ", ".join(dead)
    if snap.get("errors"):
        note += f" Bağlantı {snap['errors']} kez koptu ve yeniden kuruldu."
    return Part("market", "Piyasa verisi", max(score, 0), note)


def _fx(snap: dict[str, Any]) -> Part:
    fx = snap.get("fx")
    if fx is None:
        return Part("fx", "Dolar kuru", 0, "USD/TRY kuru henüz yok; yeni işlem açılmaz.")
    if not fx["fresh"]:
        return Part("fx", "Dolar kuru", 3, "Kur eski; güvenlik için yeni işlem açılmaz.")
    return Part("fx", "Dolar kuru", 10, f"Güncel: 1 USD = {fx['usd_try']} TL.")


def _protection(settings: Settings) -> Part:
    score, notes = 10.0, []
    lim = settings.risk
    for field, spec in _SPEC_LIMITS.items():
        if _dec(getattr(lim, field)) > spec:
            score -= 2
            notes.append(f"{field} gevşetilmiş ({getattr(lim, field)} > {spec})")
    if settings.trading_mode is not TradingMode.PAPER_TRADING:
        if settings.trading_mode is TradingMode.READ_ONLY:
            notes.append("sadece izleme modu")
        else:
            score -= 3
            notes.append("gerçek para modu")
    if not settings.capital_guard.enabled:
        score -= 2
        notes.append("ana para koruması kapalı")
    if settings.market_type is not MarketType.SPOT or settings.leverage != 1:
        score -= 3
        notes.append("kaldıraç/vadeli açık")
    if not notes:
        notes.append("Tüm zarar sınırları ve ana para koruması açık; spot, kaldıraçsız, deneme parası.")
    return Part("protection", "Risk korumaları", max(score, 0), "; ".join(notes))


def _headroom(snap: dict[str, Any]) -> Part:
    risk = snap.get("risk")
    if not risk:
        return Part("headroom", "Zarar sınırlarına uzaklık", 0, "Risk motoru verisi yok.")
    lim = risk["limits"]
    used = [
        ("günlük", _dec(risk.get("daily_loss_pct")), _dec(lim["max_daily_loss_pct"])),
        ("haftalık", _dec(risk.get("weekly_loss_pct")), _dec(lim["max_weekly_loss_pct"])),
        ("zirveden", _dec(risk.get("drawdown_pct")), _dec(lim["max_total_drawdown_pct"])),
    ]
    guard = risk.get("capital_guard")
    if guard:
        used.append(("ana para tabanına kalan pay", _dec(guard["used_pct"]), Decimal(100)))
    worst_name, worst = "", Decimal(0)
    for name, value, limit in used:
        share = max(value, Decimal(0)) / limit if limit > 0 else Decimal(1)
        if share > worst:
            worst_name, worst = name, share
    worst = min(worst, Decimal(1))
    score = float(10 * (1 - worst))
    if risk["halts"]:
        note = "Durduruldu: " + " | ".join(h["message"] for h in risk["halts"])
    elif worst == 0:
        note = "Hiçbir zarar sınırı kullanılmadı."
    else:
        note = f"En çok kullanılan: {worst_name} sınırın %{worst * 100:.0f}'i."
    return Part("headroom", "Zarar sınırlarına uzaklık", score, note)


def _notifications(links: Links) -> Part:
    if links.telegram_token and links.telegram_chat:
        return Part("telegram", "Telefon bildirimleri", 10, "Telegram (Achilles) bağlı.")
    if links.telegram_token:
        return Part("telegram", "Telefon bildirimleri", 5,
                    "Telegram anahtarı var ama kimlik numarası (chat id) eksik.")
    return Part("telegram", "Telefon bildirimleri", 0, "Telegram kurulmamış; uyarılar telefona gitmez.")


def _jarvis(settings: Settings, links: Links) -> Part:
    if not settings.jarvis.enabled:
        return Part("jarvis", "Jarvis bağlantısı", 7, "Ayarlarda kapalı (isteğe bağlı).")
    if not links.jarvis_api:
        return Part("jarvis", "Jarvis bağlantısı", 3, "Açık olması gerekiyordu ama başlatılamadı.")
    extra = " Olaylar Jarvis'e de gönderiliyor." if links.jarvis_webhook else ""
    return Part("jarvis", "Jarvis bağlantısı", 10, "Okuma ve acil durdurma bağlantısı hazır." + extra)


def _strategy(settings: Settings, snap: dict[str, Any], now: datetime) -> Part:
    st = snap.get("strategy")
    if not settings.strategy.enabled or st is None:
        return Part("strategy", "Strateji verisi", 7, "Strateji kapalı; yeni alış yapılmaz.")
    pairs = st.get("pairs") or {}
    if not pairs:
        return Part("strategy", "Strateji verisi", 0, "Strateji için çift yok.")
    # Candles are stamped with their open time: the last closed one opened at most
    # two bars ago, so three bars leaves room for a slow refresh.
    late_after = timedelta(minutes=3 * settings.strategy.bar_minutes)
    good, bad = 0, []
    for pair, info in pairs.items():
        ts = info.get("last_candle")
        fresh = ts is not None and now - datetime.fromisoformat(ts) <= late_after
        if info.get("ready") and fresh:
            good += 1
        else:
            bad.append(pair)
    note = "Mum verisi tam ve güncel." if not bad else "Hazır değil: " + ", ".join(bad)
    return Part("strategy", "Strateji verisi", 10 * good / len(pairs), note)


def health(settings: Settings, snap: dict[str, Any], links: Links, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    parts = [
        _protection(settings),
        _headroom(snap),
        _market(snap),
        _fx(snap),
        _strategy(settings, snap, now),
        _notifications(links),
        _jarvis(settings, links),
    ]
    score = sum(p.score for p in parts) / len(parts)
    weak = [p.name for p in parts if p.score < TARGET]
    return {
        "score": round(score, 1),
        "pct": round(score * 10),
        "target": TARGET,
        "ok": score >= TARGET and not weak,
        "summary": ("Tüm parçalar hedefte." if not weak else "Hedefin altında: " + ", ".join(weak)),
        "parts": [p.to_json() for p in parts],
        "note": "Bu puan sistemin güvenli ve eksiksiz çalışıp çalışmadığını ölçer; kâr garantisi değildir.",
    }
