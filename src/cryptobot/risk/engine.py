"""Risk Engine and Kill-Switch Controller.

Every new position must pass `check_trade`. `evaluate` runs every few seconds,
tracks daily/weekly loss and drawdown, and halts the bot when a limit is hit.

Halt levels:
  NORMAL            new positions allowed (if the pair and FX checks pass)
  NO_NEW_POSITIONS  nothing new is opened; monitoring continues
  CLOSE_ALL         drawdown limit hit: nothing new, open positions must be closed
                    (paper positions arrive in Phase 3, which acts on this flag)

Who can clear what:
  daily / weekly loss halt   only time (next day / next Monday, Turkey time)
  manual stop, delist halt   the owner with /devam
  drawdown halt              the owner with "/devam ONAY" (resets the peak)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol

from cryptobot.config import RISK_LIMIT_FIELDS, RiskLimits, Settings
from cryptobot.domain.models import Symbol, TradingMode
from cryptobot.events import EventLog, Level
from cryptobot.fmt import fmt_pct, fmt_try
from cryptobot.market_data.fx import FxRateService
from cryptobot.market_data.service import MarketDataService
from cryptobot.risk.state import RiskState, RiskStateError, load_state, save_state

log = logging.getLogger(__name__)

_LIMIT_LABELS = {
    "max_concurrent_positions": "Aynı anda en fazla pozisyon",
    "max_daily_loss_pct": "Günlük en fazla zarar %",
    "max_weekly_loss_pct": "Haftalık en fazla zarar %",
    "max_total_drawdown_pct": "Zirveden en fazla düşüş %",
    "max_risk_per_trade_pct": "İşlem başına en fazla risk %",
}


class HaltLevel(IntEnum):
    NORMAL = 0
    NO_NEW_POSITIONS = 1
    CLOSE_ALL = 2


class PortfolioView(Protocol):
    """What the risk engine needs from the portfolio. Phase 3 plugs in paper PnL."""

    def equity(self) -> Decimal:
        """Total account value in the risk currency (TRY)."""

    def open_symbols(self) -> set[Symbol]: ...


class FlatPortfolio:
    """Phase 2 stand-in: no trading yet, so equity stays at the starting capital."""

    def __init__(self, capital: Decimal) -> None:
        self._capital = capital

    def equity(self) -> Decimal:
        return self._capital

    def open_symbols(self) -> set[Symbol]:
        return set()


@dataclass(frozen=True, slots=True)
class Halt:
    kind: str
    level: HaltLevel
    message: str


@dataclass(frozen=True, slots=True)
class TradeIntent:
    symbol: Symbol
    quantity: Decimal  # base currency units
    entry_price: Decimal  # quote currency
    stop_price: Decimal | None  # where the trade is wrong; required
    side: str = "LONG"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    risk_amount: Decimal | None = None  # in risk currency
    risk_pct: Decimal | None = None


def _pct(part: Decimal, whole: Decimal | None) -> Decimal:
    if not whole or whole <= 0:
        return Decimal(0)
    return max(Decimal(0), part / whole * 100)


class RiskEngine:
    def __init__(
        self,
        settings: Settings,
        md: MarketDataService,
        fx: FxRateService,
        portfolio: PortfolioView,
        events: EventLog,
        state_path: Path | None,
        live_pairs: Iterable[Symbol] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._settings = settings
        self._md = md
        self._fx = fx
        self._portfolio = portfolio
        self._events = events
        self._state_path = state_path
        self._live: set[Symbol] = set(live_pairs if live_pairs is not None else md.pairs)
        self._started_at = started_at or datetime.now(timezone.utc)
        self._offset = timedelta(hours=settings.risk.reset_utc_offset_hours)
        self._warned: set[str] = set()
        self._fx_ok: bool | None = None
        self._pair_problems: dict[Symbol, list[str]] = {}
        self._alerted_pairs: dict[Symbol, list[str]] = {}
        self._blocked_since: dict[Symbol, datetime] = {}
        self._last_eval: dict[str, Any] = {}

        try:
            self._state = load_state(state_path) if state_path else RiskState()
        except RiskStateError as exc:
            self._state = RiskState(manual_stop="Risk durum dosyası okunamadı")
            events.emit(
                Level.KRITIK,
                "state_unreadable",
                "Risk durum dosyası bozuk veya okunamadı. Güvenlik için yeni işlem durduruldu. "
                "Kontrol edip /devam yazabilirsiniz.",
                error=str(exc),
            )
        self.pending_limits: RiskLimits | None = None
        self.limits = self._resolve_limits(settings.risk)
        self._save()

    # ------------------------------------------------------------------ limits

    def _resolve_limits(self, configured: RiskLimits) -> RiskLimits:
        """Tighter limits apply at once; looser ones wait for /onayla (audit + approval)."""
        approved = self._state.approved_limits
        new = {f: str(getattr(configured, f)) for f in RISK_LIMIT_FIELDS}
        if approved is None:
            self._state.approved_limits = new
            return configured
        if approved == new:
            return configured
        changes, looser = [], False
        merged: dict[str, Any] = {}
        for f in RISK_LIMIT_FIELDS:
            old_v, new_v = Decimal(approved[f]), Decimal(new[f])
            merged[f] = min(old_v, new_v)
            if old_v != new_v:
                changes.append(f"{_LIMIT_LABELS[f]}: {old_v} → {new_v}")
                looser = looser or new_v > old_v
        if looser:
            self.pending_limits = configured
            self._events.emit(
                Level.UYARI,
                "limits_pending",
                "Ayar dosyasında risk limitleri GEVŞETİLMİŞ:\n"
                + "\n".join(changes)
                + "\nOnaylanana kadar eski (daha sıkı) değerler geçerli. Onaylamak için Telegram'dan /onayla yazın.",
                changes=changes,
            )
            merged["max_concurrent_positions"] = int(merged["max_concurrent_positions"])
            return RiskLimits.model_validate({**configured.model_dump(), **merged})
        self._state.approved_limits = new
        self._events.emit(
            Level.UYARI,
            "limits_changed",
            "Risk limitleri sıkılaştırıldı ve hemen uygulandı:\n" + "\n".join(changes),
            changes=changes,
        )
        return configured

    def approve_limits(self) -> str:
        if self.pending_limits is None:
            return "Onay bekleyen bir limit değişikliği yok."
        self.limits = self.pending_limits
        self._state.approved_limits = {
            f: str(getattr(self.limits, f)) for f in RISK_LIMIT_FIELDS
        }
        self.pending_limits = None
        self._save()
        self._events.emit(
            Level.UYARI, "limits_approved", "Yeni (daha gevşek) risk limitleri onaylandı ve uygulandı."
        )
        return "Yeni risk limitleri onaylandı ve uygulandı."

    # ------------------------------------------------------------------ time

    def _local(self, now: datetime) -> datetime:
        return now + self._offset

    def day_key(self, now: datetime) -> str:
        return self._local(now).date().isoformat()

    def week_key(self, now: datetime) -> str:
        year, week, _ = self._local(now).isocalendar()
        return f"{year}-W{week:02d}"

    # ------------------------------------------------------------------ helpers

    def to_try(self, amount: Decimal, currency: str) -> Decimal | None:
        if currency == self._settings.risk_currency:
            return amount
        usd = self._fx.to_usd(amount, currency)
        rate = self._fx.usd_try()
        if usd is None or rate is None:
            return None
        return usd * rate.rate

    def set_live_pairs(self, live: Iterable[Symbol]) -> None:
        self._live = set(live)

    def _save(self) -> None:
        if self._state_path is not None:
            try:
                save_state(self._state_path, self._state)
            except OSError as exc:
                log.error("cannot save risk state: %s", exc)

    # ------------------------------------------------------------------ halts

    def halts(self) -> list[Halt]:
        s, out = self._state, []
        if s.drawdown_halt:
            out.append(Halt("drawdown", HaltLevel.CLOSE_ALL,
                            "Zirveden düşüş limiti aşıldı (acil durdurma)"))
        if s.weekly_halt_week:
            out.append(Halt("weekly", HaltLevel.NO_NEW_POSITIONS,
                            "Haftalık zarar limiti doldu (pazartesi kendiliğinden açılır)"))
        if s.daily_halt_day:
            out.append(Halt("daily", HaltLevel.NO_NEW_POSITIONS,
                            "Günlük zarar limiti doldu (gece 00:00'da kendiliğinden açılır)"))
        for pair in s.delist_halts:
            out.append(Halt("delist", HaltLevel.NO_NEW_POSITIONS,
                            f"{pair} borsada işleme kapandı, o çiftte pozisyon vardı"))
        if s.manual_stop:
            out.append(Halt("manual", HaltLevel.NO_NEW_POSITIONS, f"Elle durduruldu: {s.manual_stop}"))
        return out

    @property
    def halt_level(self) -> HaltLevel:
        return max((h.level for h in self.halts()), default=HaltLevel.NORMAL)

    @property
    def positions_must_close(self) -> bool:
        return self.halt_level is HaltLevel.CLOSE_ALL

    def stop(self, reason: str, by: str = "owner") -> str:
        """Manual kill switch level 1. `by` is "owner" (Telegram) or "jarvis"."""
        if self._state.manual_stop:
            return "Bot zaten durdurulmuş durumda."
        self._state.manual_stop = reason
        self._state.manual_stop_by = by
        self._save()
        who = "Jarvis" if by == "jarvis" else "Siz (Telegram)"
        self._events.emit(
            Level.UYARI,
            "manual_stop",
            f"{who} yeni işlem açmayı durdurdu. Sebep: {reason}\n"
            "Bot çalışmaya ve izlemeye devam ediyor. Kaldırmak için /devam.",
            notify=True, key=by, reason=reason, by=by,
        )
        return (
            "Durduruldu. Bot artık yeni işlem açmayacak ama fiyatları ve riskleri izlemeye "
            "devam ediyor. Tekrar başlatmak için /devam yazın."
        )

    def resume(self, confirm: bool = False, by: str = "owner") -> str:
        """The owner can clear manual and delist halts (and drawdown with confirm).
        Jarvis can only lift a stop that Jarvis itself made."""
        s = self._state
        notes: list[str] = []
        cleared: list[str] = []
        if by != "owner":
            if s.manual_stop and s.manual_stop_by == by:
                s.manual_stop = s.manual_stop_by = None
                cleared.append("Jarvis'in durdurması")
            elif s.manual_stop:
                notes.append("Bu durdurmayı sahibi yaptı; sadece Telegram'dan /devam ile kaldırılabilir.")
            if s.drawdown_halt or s.delist_halts:
                notes.append("Acil durdurma ve listeden çıkma güvenli modunu sadece sahibi kaldırabilir.")
            confirm = False
        else:
            if s.manual_stop:
                s.manual_stop = s.manual_stop_by = None
                cleared.append("elle durdurma")
            if s.delist_halts:
                cleared.append("listeden çıkma güvenli modu (" + ", ".join(s.delist_halts) + ")")
                s.delist_halts = []
        if s.drawdown_halt and by == "owner":
            if confirm:
                s.drawdown_halt = False
                s.peak_equity = self._portfolio.equity()
                cleared.append("acil durdurma (zirve, şu anki bakiyeye sıfırlandı)")
            else:
                notes.append(
                    "Acil durdurma (zirveden düşüş limiti) hâlâ açık. Bunu kaldırmak büyük bir "
                    "karar: emin iseniz \"/devam ONAY\" yazın."
                )
        if s.weekly_halt_week:
            notes.append("Haftalık zarar limiti doldu; pazartesi kendiliğinden açılacak, elle kaldırılamaz.")
        if s.daily_halt_day:
            notes.append("Günlük zarar limiti doldu; gece 00:00'da kendiliğinden açılacak, elle kaldırılamaz.")
        self._save()
        if cleared:
            who = "Jarvis" if by == "jarvis" else "Siz (Telegram)"
            self._events.emit(
                Level.UYARI, "resumed", f"{who} devam ettirdi: " + ", ".join(cleared) + ".",
                notify=True, key=by, by=by,
            )
        head = ("Kaldırıldı: " + ", ".join(cleared) + ".") if cleared else "Kaldırılacak bir elle durdurma yok."
        return "\n".join([head, *notes]) if notes else head + " Bot normal çalışıyor."

    # ------------------------------------------------------------------ periodic

    def evaluate(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        s = self._state
        equity = self._portfolio.equity()
        changed = False

        day, week = self.day_key(now), self.week_key(now)
        if s.day_key != day:
            if s.daily_halt_day and s.daily_halt_day != day:
                s.daily_halt_day = None
                self._events.emit(Level.BILGI, "daily_reset",
                                  "Yeni gün başladı, günlük zarar limiti sıfırlandı.", notify=True)
            s.day_key, s.day_start_equity, changed = day, equity, True
        if s.week_key != week:
            if s.weekly_halt_week and s.weekly_halt_week != week:
                s.weekly_halt_week = None
                self._events.emit(Level.BILGI, "weekly_reset",
                                  "Yeni hafta başladı, haftalık zarar limiti sıfırlandı.", notify=True)
            s.week_key, s.week_start_equity, changed = week, equity, True
        if s.peak_equity is None or equity > s.peak_equity:
            s.peak_equity, changed = equity, True

        assert s.day_start_equity is not None and s.week_start_equity is not None
        daily = _pct(s.day_start_equity - equity, s.day_start_equity)
        weekly = _pct(s.week_start_equity - equity, s.week_start_equity)
        drawdown = _pct(s.peak_equity - equity, s.peak_equity)
        lim = self.limits

        if daily >= lim.max_daily_loss_pct and s.daily_halt_day != day:
            s.daily_halt_day, changed = day, True
            self._events.emit(Level.KRITIK, "daily_loss_halt",
                              f"Günlük zarar limiti doldu ({fmt_pct(daily)} / {fmt_pct(lim.max_daily_loss_pct)}). "
                              "Bugün yeni işlem açılmayacak.", loss_pct=daily)
        if weekly >= lim.max_weekly_loss_pct and s.weekly_halt_week != week:
            s.weekly_halt_week, changed = week, True
            self._events.emit(Level.KRITIK, "weekly_loss_halt",
                              f"Haftalık zarar limiti doldu ({fmt_pct(weekly)} / {fmt_pct(lim.max_weekly_loss_pct)}). "
                              "Pazartesiye kadar yeni işlem açılmayacak.", loss_pct=weekly)
        if drawdown >= lim.max_total_drawdown_pct and not s.drawdown_halt:
            s.drawdown_halt, changed = True, True
            self._events.emit(Level.KRITIK, "drawdown_halt",
                              f"ACİL DURDURMA: bakiye zirveden {fmt_pct(drawdown)} düştü "
                              f"(limit {fmt_pct(lim.max_total_drawdown_pct)}). Yeni işlem yok, açık "
                              "pozisyonlar kapatılacak. Kaldırmak için \"/devam ONAY\".",
                              drawdown_pct=drawdown)

        for name, value, limit, key in (
            ("Günlük zarar", daily, lim.max_daily_loss_pct, day),
            ("Haftalık zarar", weekly, lim.max_weekly_loss_pct, week),
            ("Zirveden düşüş", drawdown, lim.max_total_drawdown_pct, "dd"),
        ):
            wkey = f"{name}:{key}"
            if limit * lim.warn_at_fraction <= value < limit and wkey not in self._warned:
                self._warned.add(wkey)
                self._events.emit(Level.UYARI, "limit_near",
                                  f"{name} limite yaklaşıyor: {fmt_pct(value)} / {fmt_pct(limit)}.",
                                  key=name, value_pct=value)

        changed |= self._check_markets(now)
        if changed:
            self._save()

        self._last_eval = {
            "time": now.isoformat(timespec="seconds"),
            "equity": equity,
            "daily_loss_pct": daily,
            "weekly_loss_pct": weekly,
            "drawdown_pct": drawdown,
        }
        return self.snapshot(now)

    def _check_markets(self, now: datetime) -> bool:
        """FX freshness, pair health and delisting. Alerts only on changes."""
        changed = False
        in_grace = (now - self._started_at).total_seconds() < self._settings.status.alert_grace_seconds

        fx_ok = self._fx.is_fresh(now)
        if fx_ok != self._fx_ok and not (in_grace and not fx_ok):
            if fx_ok and self._fx_ok is not None:
                self._events.emit(Level.BILGI, "fx_ok", "USD/TRY kuru tekrar güncel.", notify=True)
            elif not fx_ok:
                self._events.emit(Level.UYARI, "fx_stale",
                                  "USD/TRY kuru 5 dakikadan eski veya yok. Yeni pozisyon açılmayacak.")
            self._fx_ok = fx_ok

        open_symbols = self._portfolio.open_symbols()
        for sym in self._md.pairs:
            problems = self.pair_problems(sym, now)
            self._pair_problems[sym] = problems
            if sym not in self._live and sym in open_symbols and str(sym) not in self._state.delist_halts:
                self._state.delist_halts.append(str(sym))
                changed = True
                self._events.emit(Level.KRITIK, "delist_open_position",
                                  f"{sym} borsada işleme kapandı ve bu çiftte açık pozisyon var! "
                                  "Bot güvenli moda geçti, yeni işlem açılmayacak.", key=str(sym))
            if in_grace:
                continue
            # Thin TRY books flicker; only alert after the pair stays blocked for a while.
            if problems:
                since = self._blocked_since.setdefault(sym, now)
                held = (now - since).total_seconds() >= self._settings.liquidity.alert_after_seconds
                if held and sym not in self._alerted_pairs:
                    self._alerted_pairs[sym] = problems
                    self._events.emit(Level.UYARI, "pair_blocked",
                                      f"{sym} şu an işlem için uygun değil: " + "; ".join(problems),
                                      key=str(sym), problems=problems)
            else:
                self._blocked_since.pop(sym, None)
                if self._alerted_pairs.pop(sym, None) is not None:
                    self._events.emit(Level.BILGI, "pair_ok", f"{sym} tekrar işlem için uygun.",
                                      notify=True, key=str(sym))
        return changed

    def pair_problems(self, sym: Symbol, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        liq = self._settings.liquidity
        problems: list[str] = []
        if sym not in self._live:
            problems.append("borsada işleme kapalı veya listeden çıkmış")
        ticker = self._md.ticker(sym)
        if ticker is None or self._md.is_stale(sym, now):
            problems.append("fiyat verisi yok veya eski")
            return problems
        if ticker.spread_bps > liq.max_spread_bps:
            problems.append(f"alış-satış farkı geniş ({ticker.spread_bps:.1f} bps, sınır {liq.max_spread_bps})")
        volume = self.to_try(ticker.volume_24h_quote, sym.quote)
        if volume is None:
            problems.append("hacmin TL karşılığı hesaplanamadı")
        elif volume < liq.min_24h_volume_try:
            problems.append(f"24 saatlik hacim düşük ({fmt_try(volume)}, sınır {fmt_try(liq.min_24h_volume_try)})")
        book = self._md.book(sym)
        max_age = self._settings.market_data.stale_after_seconds
        if book is None or (now - book.received_ts).total_seconds() > max_age:
            problems.append("emir defteri verisi yok veya eski")
        else:
            depth_q = min(book.depth_quote("bid", liq.depth_band_bps),
                          book.depth_quote("ask", liq.depth_band_bps))
            depth = self.to_try(depth_q, sym.quote)
            if depth is None or depth < liq.min_book_depth_try:
                problems.append(f"emir defteri sığ ({fmt_try(depth)}, sınır {fmt_try(liq.min_book_depth_try)})")
        return problems

    # ------------------------------------------------------------------ pre-trade

    def check_trade(self, intent: TradeIntent, now: datetime | None = None) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        reasons = [h.message for h in self.halts()]
        if self._settings.trading_mode is TradingMode.READ_ONLY:
            reasons.append("Bot sadece-izleme (READ_ONLY) modunda")
        if intent.side != "LONG":
            reasons.append("Spot piyasada sadece alış (LONG) yapılabilir")
        if not self._fx.is_fresh(now):
            reasons.append("USD/TRY kuru 5 dakikadan eski")
        reasons += [f"{intent.symbol}: {p}" for p in self.pair_problems(intent.symbol, now)]

        open_symbols = self._portfolio.open_symbols()
        if intent.symbol in open_symbols:
            reasons.append(f"{intent.symbol} için zaten açık pozisyon var")
        elif len(open_symbols) >= self.limits.max_concurrent_positions:
            reasons.append(f"Aynı anda en fazla {self.limits.max_concurrent_positions} pozisyon açılabilir")

        risk_amount = risk_pct = None
        if intent.quantity <= 0 or intent.entry_price <= 0:
            reasons.append("Miktar ve fiyat sıfırdan büyük olmalı")
        elif intent.stop_price is None or intent.stop_price <= 0 or intent.stop_price >= intent.entry_price:
            reasons.append("Geçerli bir zarar-kes (stop) fiyatı yok")
        else:
            equity = self._portfolio.equity()
            per_unit = self._per_unit_risk(intent.entry_price, intent.stop_price)
            risk_amount = self.to_try(intent.quantity * per_unit, intent.symbol.quote)
            notional = self.to_try(intent.quantity * intent.entry_price, intent.symbol.quote)
            if risk_amount is None or notional is None:
                reasons.append("Riskin TL karşılığı hesaplanamadı")
            else:
                risk_pct = _pct(risk_amount, equity)
                if risk_pct > self.limits.max_risk_per_trade_pct:
                    reasons.append(f"İşlem riski çok yüksek ({fmt_pct(risk_pct)}, sınır "
                                   f"{fmt_pct(self.limits.max_risk_per_trade_pct)})")
                if notional > equity:
                    reasons.append("İşlem büyüklüğü bakiyeyi aşıyor (kaldıraç yok)")
        return RiskDecision(not reasons, reasons, risk_amount, risk_pct)

    def _per_unit_risk(self, entry: Decimal, stop: Decimal) -> Decimal:
        cost = entry * self.limits.assumed_round_trip_cost_bps / Decimal(10_000)
        return entry - stop + cost

    def max_quantity(self, symbol: Symbol, entry: Decimal, stop: Decimal) -> Decimal | None:
        """Largest size that keeps the trade within the per-trade risk limit."""
        if stop <= 0 or stop >= entry:
            return None
        equity = self._portfolio.equity()
        budget = equity * self.limits.max_risk_per_trade_pct / 100
        per_unit = self.to_try(self._per_unit_risk(entry, stop), symbol.quote)
        price = self.to_try(entry, symbol.quote)
        if not per_unit or not price:
            return None
        return min(budget / per_unit, equity / price)

    # ------------------------------------------------------------------ views

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        ev = self._last_eval
        lim = self.limits
        s = self._state

        def num(d: Any) -> str | None:
            return None if d is None else f"{d:.2f}"

        return {
            "halt_level": self.halt_level.name,
            "halts": [{"kind": h.kind, "level": h.level.name, "message": h.message} for h in self.halts()],
            "new_positions_allowed": self.halt_level is HaltLevel.NORMAL and self._fx.is_fresh(now)
            and self._settings.trading_mode is not TradingMode.READ_ONLY,
            "fx_fresh": self._fx.is_fresh(now),
            "equity": num(ev.get("equity")),
            "peak_equity": num(s.peak_equity),
            "daily_loss_pct": num(ev.get("daily_loss_pct")),
            "weekly_loss_pct": num(ev.get("weekly_loss_pct")),
            "drawdown_pct": num(ev.get("drawdown_pct")),
            "open_positions": len(self._portfolio.open_symbols()),
            "limits": {f: str(getattr(lim, f)) for f in RISK_LIMIT_FIELDS},
            "limits_pending_approval": self.pending_limits is not None,
            "pairs": {str(sym): {"tradable": not p, "problems": p} for sym, p in self._pair_problems.items()},
        }

    def limits_text(self) -> str:
        ev, lim = self._last_eval, self.limits
        zero = Decimal(0)
        lines = [
            "Risk limitleri (kullanılan / sınır):",
            f"• Günlük zarar: {fmt_pct(ev.get('daily_loss_pct', zero))} / {fmt_pct(lim.max_daily_loss_pct)}",
            f"• Haftalık zarar: {fmt_pct(ev.get('weekly_loss_pct', zero))} / {fmt_pct(lim.max_weekly_loss_pct)}",
            f"• Zirveden düşüş: {fmt_pct(ev.get('drawdown_pct', zero))} / {fmt_pct(lim.max_total_drawdown_pct)}",
            f"• İşlem başına risk: en fazla {fmt_pct(lim.max_risk_per_trade_pct)}",
            f"• Açık pozisyon: {len(self._portfolio.open_symbols())} / {lim.max_concurrent_positions}",
            f"• Kur en fazla {int(self._settings.fx.max_age_seconds // 60)} dakika eski olabilir",
        ]
        if self.pending_limits is not None:
            lines.append("Not: ayar dosyasında onay bekleyen gevşek limitler var (/onayla).")
        return "\n".join(lines)
