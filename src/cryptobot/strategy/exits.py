"""Exit module, independent of the entry strategy.

Rules, checked when a bar closes (the stop itself is also watched in between):
  * ATR stop: the first stop comes from the entry signal (stop_atr x ATR below entry).
  * Break-even: once the best close is `breakeven_at_r` R above entry, the stop moves
    to entry + a small buffer for costs. R = entry - first stop.
  * Trailing: after break-even the stop follows the best close at `trail_atr` x ATR.
    The stop only ever moves up.
  * Time exit: sell a trade that never reached break-even after `stale_after_bars`,
    and any trade after `max_hold_bars`.
  * Trend exit: sell when the higher-timeframe trend turns down (optional).
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptobot.config import ExitConfig


@dataclass(slots=True)
class ExitState:
    entry: float
    initial_stop: float
    stop: float
    highest: float  # best close since entry
    bars_held: int = 0
    breakeven: bool = False


@dataclass(frozen=True, slots=True)
class ExitDecision:
    exit_reason: str | None  # None = keep; "stop-loss" or "satış"
    note: str | None  # Turkish explanation
    stop_moved: bool = False
    moved_to_breakeven: bool = False


def on_bar_close(cfg: ExitConfig, st: ExitState, close: float, atr: float | None,
                 trend_ok: bool | None) -> ExitDecision:
    """Update `st` for a newly closed bar (caller sets st.bars_held) and decide."""
    old_stop = st.stop
    st.highest = max(st.highest, close)
    risk = st.entry - st.initial_stop
    to_be = False
    if not st.breakeven and risk > 0 and st.highest >= st.entry + cfg.breakeven_at_r * risk:
        st.breakeven = to_be = True
        st.stop = max(st.stop, st.entry * (1 + cfg.breakeven_buffer_bps / 10_000))
    if st.breakeven and atr:
        st.stop = max(st.stop, st.highest - cfg.trail_atr * atr)
    moved = st.stop > old_stop

    if close <= st.stop:
        reason = "stop-loss" if st.stop < st.entry else "satış"
        return ExitDecision(reason, "fiyat zarar-kes / iz süren stop seviyesine indi", moved, to_be)
    if cfg.exit_on_trend_break and trend_ok is False:
        return ExitDecision("satış", "büyük resimdeki yükseliş trendi bozuldu", moved, to_be)
    if st.bars_held >= cfg.max_hold_bars:
        return ExitDecision("satış", "en uzun tutma süresi doldu", moved, to_be)
    if not st.breakeven and st.bars_held >= cfg.stale_after_bars:
        return ExitDecision("satış", "işlem beklenen sürede ilerlemedi", moved, to_be)
    return ExitDecision(None, None, moved, to_be)
