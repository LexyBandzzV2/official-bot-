"""Profit-leakage analysis by strategy mode — Phase 4.

Reads closed ``TradeRecord`` objects (which carry the Phase 3/4 lifecycle fields)
and produces per-mode stats that reveal where realised profit falls short of MFE.

Typical usage (programmatic)::

    from src.backtest.leakage_analyzer import analyze_leakage_by_mode, print_leakage_table
    result = analyze_leakage_by_mode(trades)
    print_leakage_table(result)

Stats computed per mode
-----------------------
count                  — number of closed trades in this mode
win_rate               — fraction with pnl_pct > 0
avg_mfe                — average max unrealized profit %
avg_realized_pnl       — average realised pnl_pct
avg_giveback           — avg_mfe  − avg_realized_pnl  (profit leakage %pts)
avg_capture_ratio      — avg(pnl / mfe) for trades where mfe > 0
protected_profit_rate  — fraction where was_protected_profit=True
be_armed_rate          — fraction where break_even_armed=True
stage_1_rate           — fraction reaching profit_lock_stage >= 1
stage_2_rate           — fraction reaching profit_lock_stage >= 2
stage_3_rate           — fraction reaching profit_lock_stage >= 3
candle_trail_rate      — fraction with trail_active_mode in ("candle_trail", "candle_structure_trail")
atr_trail_rate         — fraction with trail_active_mode == "atr_trail"
momentum_fade_rate     — fraction with trail_active_mode == "momentum_fade"
fallback_policy_rate   — fraction with used_fallback_policy=True
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_MODES = ("SCALP", "INTERMEDIATE", "SWING")


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _rate(trade_list: list, attr: str, test_fn) -> float:
    """Return fraction of *trade_list* where test_fn(getattr(t, attr)) is True."""
    if not trade_list:
        return 0.0
    return sum(1 for t in trade_list if test_fn(getattr(t, attr, None))) / len(trade_list)


def _compute_mode_stats(mode_trades: list[Any]) -> dict:
    """Compute leakage stats for a single mode's closed trade list."""
    n = len(mode_trades)
    if n == 0:
        return {
            "count": 0,
            "win_rate": 0.0,
            "avg_mfe": 0.0,
            "avg_realized_pnl": 0.0,
            "avg_giveback": 0.0,
            "avg_capture_ratio": 0.0,
            "protected_profit_rate": 0.0,
            "be_armed_rate": 0.0,
            "stage_1_rate": 0.0,
            "stage_2_rate": 0.0,
            "stage_3_rate": 0.0,
            "candle_trail_rate": 0.0,
            "atr_trail_rate": 0.0,
            "momentum_fade_rate": 0.0,
            "fallback_policy_rate": 0.0,
        }

    pnls      = [getattr(t, "pnl_pct",               0.0) for t in mode_trades]
    mfes      = [getattr(t, "max_unrealized_profit",  0.0) for t in mode_trades]
    winners   = [p for p in pnls if p > 0]
    win_rate  = len(winners) / n

    avg_mfe           = _safe_mean(mfes)
    avg_realized      = _safe_mean(pnls)
    avg_giveback      = avg_mfe - avg_realized

    # Capture ratio only for trades where MFE was meaningfully positive
    cap_pairs = [
        (getattr(t, "pnl_pct", 0.0), getattr(t, "max_unrealized_profit", 0.0))
        for t in mode_trades
        if getattr(t, "max_unrealized_profit", 0.0) > 0.0
    ]
    if cap_pairs:
        avg_capture_ratio = _safe_mean([p / m for p, m in cap_pairs])
    else:
        avg_capture_ratio = 0.0

    # Protection rates
    protected_profit_rate = _rate(mode_trades, "was_protected_profit",  bool)
    be_armed_rate         = _rate(mode_trades, "break_even_armed",       bool)
    stage_1_rate          = _rate(mode_trades, "profit_lock_stage",      lambda s: (s or 0) >= 1)
    stage_2_rate          = _rate(mode_trades, "profit_lock_stage",      lambda s: (s or 0) >= 2)
    stage_3_rate          = _rate(mode_trades, "profit_lock_stage",      lambda s: (s or 0) >= 3)

    # Trail mode rates
    candle_trail_rate = _rate(
        mode_trades, "trail_active_mode",
        lambda m: m in ("candle_trail", "candle_structure_trail")
    )
    atr_trail_rate = _rate(
        mode_trades, "trail_active_mode",
        lambda m: m == "atr_trail"
    )
    momentum_fade_rate = _rate(
        mode_trades, "trail_active_mode",
        lambda m: m == "momentum_fade"
    )

    # Fallback policy
    fallback_policy_rate = _rate(mode_trades, "used_fallback_policy", bool)

    return {
        "count":                 n,
        "win_rate":              win_rate,
        "avg_mfe":               avg_mfe,
        "avg_realized_pnl":      avg_realized,
        "avg_giveback":          avg_giveback,
        "avg_capture_ratio":     avg_capture_ratio,
        "protected_profit_rate": protected_profit_rate,
        "be_armed_rate":         be_armed_rate,
        "stage_1_rate":          stage_1_rate,
        "stage_2_rate":          stage_2_rate,
        "stage_3_rate":          stage_3_rate,
        "candle_trail_rate":     candle_trail_rate,
        "atr_trail_rate":        atr_trail_rate,
        "momentum_fade_rate":    momentum_fade_rate,
        "fallback_policy_rate":  fallback_policy_rate,
    }


def analyze_leakage_by_mode(trades: list[Any]) -> dict[str, dict]:
    """Return per-mode leakage stats for all closed trades in *trades*.

    Parameters
    ----------
    trades:
        List of ``TradeRecord`` objects (open or closed; open ones are skipped).

    Returns
    -------
    dict mapping each mode label ("SCALP", "INTERMEDIATE", "SWING") to its
    stats dict.  Modes with zero closed trades receive a zero-filled stats dict.
    """
    closed = [t for t in trades if getattr(t, "status", "") == "CLOSED"]
    result: dict[str, dict] = {}
    for mode in _MODES:
        mode_trades = [
            t for t in closed
            if getattr(t, "strategy_mode", "UNKNOWN") == mode
        ]
        result[mode] = _compute_mode_stats(mode_trades)
    return result


def print_leakage_table(result: dict[str, dict]) -> None:
    """Print a plain-text profit-leakage table for each mode.

    Compatible with any environment — no Rich dependency.
    """
    _W = 22
    _N = 9

    def _pct(v: float) -> str:
        return f"{v:+.2f}%"

    def _rate_str(v: float) -> str:
        return f"{v*100:.1f}%"

    header = (
        f"{'Mode':<14} {'Trades':>{_N}} {'Win%':>{_N}} "
        f"{'AvgMFE':>{_N}} {'AvgPnL':>{_N}} {'AvgGvbk':>{_N}} "
        f"{'CapRatio':>{_N}} {'BE%':>{_N}} {'Stg1%':>{_N}} "
        f"{'CndlTrl%':>{_N}} {'AtrTrl%':>{_N}} {'Fallback%':>{_N}}"
    )
    sep = "-" * len(header)
    lines = [sep, header, sep]
    for mode in _MODES:
        s = result.get(mode, {})
        if not s or s.get("count", 0) == 0:
            lines.append(f"{mode:<14} {'0':>{_N}}")
            continue
        lines.append(
            f"{mode:<14} "
            f"{s['count']:>{_N}} "
            f"{_rate_str(s['win_rate']):>{_N}} "
            f"{_pct(s['avg_mfe']):>{_N}} "
            f"{_pct(s['avg_realized_pnl']):>{_N}} "
            f"{_pct(s['avg_giveback']):>{_N}} "
            f"{s['avg_capture_ratio']:>{_N}.2f} "
            f"{_rate_str(s['be_armed_rate']):>{_N}} "
            f"{_rate_str(s['stage_1_rate']):>{_N}} "
            f"{_rate_str(s['candle_trail_rate']):>{_N}} "
            f"{_rate_str(s['atr_trail_rate']):>{_N}} "
            f"{_rate_str(s['fallback_policy_rate']):>{_N}}"
        )
    lines.append(sep)
    print("\n".join(lines))
