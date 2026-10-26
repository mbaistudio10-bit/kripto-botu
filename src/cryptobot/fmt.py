"""Turkish number formatting for messages (1.234,56 TL, %2,50)."""

from __future__ import annotations

from decimal import Decimal


def fmt_num(d: Decimal, places: int = 2) -> str:
    return f"{d:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(d: Decimal) -> str:
    return "%" + fmt_num(d)


def fmt_try(d: Decimal | None) -> str:
    return "-" if d is None else fmt_num(d) + " TL"
