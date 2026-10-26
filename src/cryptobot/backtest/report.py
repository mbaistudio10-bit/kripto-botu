"""Backtest numbers and a short Turkish report the owner can read."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from cryptobot.backtest.engine import BtResult, _rate_at
from cryptobot.domain.models import Candle

DISCLAIMER = ("Bu bir geçmiş veri testidir, yatırım tavsiyesi değildir. Geçmişte işe yarayan "
              "bir kural gelecekte para kaybettirebilir; geçmiş performans gelecekteki getiriyi garanti etmez.")


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    win_rate: float
    total_return_pct: float
    profit: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None
    avg_r: float
    max_drawdown_pct: float
    fees: float
    exposure_pct: float
    usd_return_pct: float | None


def max_drawdown_pct(curve: list[tuple[object, float]], start_equity: float) -> float:
    peak, worst = start_equity, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        worst = max(worst, (peak - eq) / peak * 100 if peak else 0.0)
    return worst


def metrics(r: BtResult) -> Metrics:
    wins = [t.pnl for t in r.trades if t.pnl > 0]
    losses = [t.pnl for t in r.trades if t.pnl <= 0]
    n = len(r.trades)
    gross_loss = -sum(losses)
    usd = None
    if r.usd_start and r.usd_end:
        usd = (r.usd_end / r.usd_start - 1) * 100
    return Metrics(
        trades=n, wins=len(wins), win_rate=len(wins) / n * 100 if n else 0.0,
        total_return_pct=(r.end_equity / r.start_equity - 1) * 100,
        profit=r.end_equity - r.start_equity,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=sum(wins) / gross_loss if gross_loss > 0 else None,
        avg_r=sum(t.r_multiple for t in r.trades) / n if n else 0.0,
        max_drawdown_pct=max_drawdown_pct(r.equity_curve, r.start_equity),
        fees=sum(t.fees for t in r.trades), exposure_pct=r.exposure_pct, usd_return_pct=usd,
    )


def _tl(x: float) -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} TL"


def _pct(x: float) -> str:
    return f"%{x:+.2f}".replace(".", ",")


def section(r: BtResult) -> list[str]:
    m = metrics(r)
    days = (r.end - r.start) / timedelta(days=1)
    lines = [
        f"■ {r.label} ({r.start:%d.%m.%Y} – {r.end:%d.%m.%Y}, {days:.0f} gün)",
        f"  Başlangıç {_tl(r.start_equity)} → bitiş {_tl(r.end_equity)}  ({_pct(m.total_return_pct)})",
    ]
    if m.usd_return_pct is not None:
        lines.append(f"  Dolar bazında: {_pct(m.usd_return_pct)} (TL'nin dolara karşı değişimi dahil)")
    lines.append(f"  İşlem sayısı: {m.trades} | kazanan: {m.wins} (%{m.win_rate:.0f})")
    if m.trades:
        pf = "-" if m.profit_factor is None else f"{m.profit_factor:.2f}".replace(".", ",")
        lines += [
            f"  Ortalama kazanç {_tl(m.avg_win)} | ortalama kayıp {_tl(m.avg_loss)} | kâr faktörü {pf}",
            f"  İşlem başına ortalama sonuç: {m.avg_r:+.2f} R (1 R = stop'ta kaybedilecek tutar)".replace(".", ",", 1),
        ]
    lines += [
        f"  En büyük düşüş (zirveden): %{m.max_drawdown_pct:.2f}".replace(".", ","),
        f"  Ödenen komisyon: {_tl(m.fees)} | piyasada kalma süresi: %{m.exposure_pct:.0f}",
        f"  Sinyal: {r.signals}, limit/nakit yüzünden alınmayan: {r.skipped}",
    ]
    for sym, pct in r.buy_hold_pct.items():
        lines.append(f"  Karşılaştırma: {sym} alıp hiç satmasaydınız {_pct(pct)}")
    if r.drawdown_halts:
        lines.append(f"  Acil durdurma (zirveden düşüş limiti): {r.drawdown_halts} kez")
    for h in r.halts[:5]:
        lines.append(f"  ⚠ {h}")
    if len(r.halts) > 5:
        lines.append(f"  ⚠ ... ve {len(r.halts) - 5} risk durdurması daha")
    return lines


def render_details(is_: BtResult, oos: BtResult) -> str:
    return "\n".join(section(is_) + [""] + section(oos))


def _pf(m: Metrics) -> str:
    return "-" if m.profit_factor is None else f"{m.profit_factor:.2f}".replace(".", ",")


def _line(name: str, r: BtResult) -> str:
    m = metrics(r)
    usd = "" if m.usd_return_pct is None else f" ({_pct(m.usd_return_pct)} USD)"
    halts = f", acil durdurma {r.drawdown_halts} kez" if r.drawdown_halts else ""
    dd = f"%{m.max_drawdown_pct:.1f}".replace(".", ",")
    return (f"   {name}: {_pct(m.total_return_pct)} TL{usd} | {m.trades} işlem, kâr faktörü {_pf(m)}, "
            f"en büyük düşüş {dd}{halts}")


def _hold_pct(fx: list[Candle] | None, r: BtResult) -> float | None:
    if not fx:
        return None
    a, b = _rate_at(fx, r.start), _rate_at(fx, r.end)
    return (b / a - 1) * 100 if a and b else None


def comparison_verdict(results: list) -> str:
    good, few = [], []
    for v, is_, oos in results:
        a, b = metrics(is_), metrics(oos)
        pf_ok = b.wins > 0 if b.profit_factor is None else b.profit_factor >= 1.1  # None = no losers
        if b.total_return_pct > 0 and pf_ok:
            if b.trades < 10:
                few.append((v, b))
            elif a.total_return_pct > 0:
                good.append((v, b))
    if good:
        v, b = max(good, key=lambda x: x[1].total_return_pct)
        text = (f"\"{v.label}\" her iki dönemde de kârlı. Umut verici ama kanıt değil: önce en az birkaç "
                "hafta deneme hesabında (sahte parayla) izlenmeli. Gerçek para için erken.")
        if b.usd_return_pct is not None and b.usd_return_pct < 0:
            text += " Dikkat: dolar bazında yine de zararda."
        return text
    if few:
        v, b = few[0]
        return (f"\"{v.label}\" sınama döneminde kâr etti ama sadece {b.trades} işlem var; bu kadar az "
                "işlemle güvenilir bir sonuç çıkmaz. Hiçbir kural gerçek parayla kullanılmamalı.")
    return ("Hiçbir kural, görmediği sınama döneminde kâr etmedi. Bu verilerde bu tür trend kurallarının "
            "işe yaradığına dair kanıt yok; gerçek parayla kullanılmamalı.")


def render_comparison(results: list, header: list[str], fx: list[Candle] | None = None) -> str:
    """Short side-by-side report (fits one Telegram message)."""
    lines = [f"📈 GEÇMİŞ VERİ TESTİ: {len(results)} kural karşılaştırması", *header, ""]
    _, is0, oos0 = results[0]
    lines.append(f"Dönemler: öğrenme {is0.start:%d.%m.%Y}–{is0.end:%d.%m.%Y}, "
                 f"sınama {oos0.start:%d.%m.%Y}–{oos0.end:%d.%m.%Y} (kurallar bu dönemi hiç görmedi)")
    for label, r in (("Öğrenme", is0), ("Sınama", oos0)):
        bh = ", ".join(f"{sym.base} {_pct(p)}" for sym, p in r.buy_hold_pct.items())
        usd = _hold_pct(fx, r)
        usd_txt = f"; sadece dolar (USDT) tutmak {_pct(usd)}" if usd is not None else ""
        lines.append(f"Karşılaştırma ({label}): alıp hiç satmamak {bh}{usd_txt}")
    lines.append("")
    for n, (v, is_, oos) in enumerate(results, 1):
        lines += [f"{n}) {v.label}", _line("Öğrenme", is_), _line("Sınama ", oos)]
    lines += ["", "Sonuç: " + comparison_verdict(results),
              "Ayrıntılar: data/backtest_report.txt", "", DISCLAIMER]
    return "\n".join(lines)


def verdict(is_: BtResult, oos: BtResult) -> str:
    a, b = metrics(is_), metrics(oos)
    if b.trades < 10:
        return ("Sınama döneminde çok az işlem oldu; sonuç hakkında güvenilir bir şey söylemek için "
                "veri yetersiz. Deneme hesabında izlemeye devam edin.")
    if b.total_return_pct > 0 and a.total_return_pct > 0 and (b.profit_factor or 0) >= 1.1:
        return ("Kural her iki dönemde de kârlı görünüyor. Bu umut verici ama yeterli değil: "
                "en az birkaç hafta deneme hesabında (sahte parayla) izlenmeli.")
    if b.total_return_pct <= 0 < a.total_return_pct:
        return ("Öğrenme döneminde kârlı, görmediği sınama döneminde kârsız. Bu, kuralın geçmişe "
                "fazla uyduğunu gösterebilir. Gerçek parayla kullanılmamalı.")
    return ("Kural bu verilerde kâr üretmedi. Gerçek parayla kullanılmamalı; deneme hesabında "
            "izlenebilir veya kurallar değiştirilebilir.")


def render(is_: BtResult, oos: BtResult, header: list[str]) -> str:
    lines = ["📈 GEÇMİŞ VERİ TESTİ (backtest)", *header, ""]
    lines += section(is_) + [""] + section(oos) + [""]
    lines += ["Sonuç: " + verdict(is_, oos), "", DISCLAIMER]
    return "\n".join(lines)
