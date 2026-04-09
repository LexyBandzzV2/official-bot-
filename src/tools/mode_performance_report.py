"""Strategy-mode performance comparison report — Prompt 8.

Computes per-mode performance metrics for SCALP, INTERMEDIATE, and SWING
from closed trades in the SQLite database, and provides three output formats:

* terminal Rich tables (print_mode_performance_report)
* markdown string   (mode_performance_to_markdown)
* JSON string       (mode_performance_to_json)

Usage::

    from src.tools.mode_performance_report import (
        get_mode_performance_data,
        print_mode_performance_report,
        mode_performance_to_markdown,
        mode_performance_to_json,
    )

    data = get_mode_performance_data()
    print_mode_performance_report(data)
    md   = mode_performance_to_markdown(data)
    js   = mode_performance_to_json(data)

Conclusion logic
----------------
* **best_mode**                — highest win rate (≥ 5 trades), avg realized PnL as tie-break
* **most_leakage_mode**        — highest avg giveback (avg_mfe − avg_realized_pnl) where avg_mfe > 0.1
* **worst_exit_efficiency_mode** — lowest avg capture ratio (pnl / mfe), min 5 trades with mfe > 0

No Phase 1–7 architecture is modified.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_MODES = ("SCALP", "INTERMEDIATE", "SWING")

_MIN_TRADES_FOR_CONCLUSION = 5   # floor before a mode qualifies for ranking

_REASON_DISPLAY: dict[str, str] = {
    "PEAK_GIVEBACK_EXIT": "Peak Giveback",
    "HARD_STOP":          "Hard Stop",
    "TRAIL_STOP":         "Trail Stop",
    "ALLIGATOR_TP":       "Alligator TP",
    "MANUAL":             "Manual",
    "UNKNOWN":            "Unknown",
}

_ALL_REASONS = list(_REASON_DISPLAY.keys())


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _normalize_reason(raw: Optional[str]) -> str:
    """Collapse legacy TRAILING_TP → PEAK_GIVEBACK_EXIT; default to UNKNOWN."""
    r = (raw or "").strip()
    if r == "TRAILING_TP":
        return "PEAK_GIVEBACK_EXIT"
    return r if r in _REASON_DISPLAY else "UNKNOWN"


def _parse_dt(val: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string or pass through a datetime; return None on failure."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # Handle trailing 'Z' (Python 3.11+ accepts it, older does not)
        s = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    return None


def _duration_secs(entry_val: Any, exit_val: Any) -> Optional[float]:
    """Return the duration in seconds between entry and exit, or None."""
    entry = _parse_dt(entry_val)
    exit_ = _parse_dt(exit_val)
    if entry is None or exit_ is None:
        return None
    # Make both timezone-aware if necessary so subtraction works
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    if exit_.tzinfo is None:
        exit_ = exit_.replace(tzinfo=timezone.utc)
    delta = exit_ - entry
    secs = delta.total_seconds()
    return abs(secs) if secs != 0 else 0.0


def _fmt_duration(secs: Optional[float]) -> str:
    """Format seconds as 'Xh Ym', or '—' if None."""
    if secs is None:
        return "—"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h}h {m}m"


def _pct(v: Any, signed: bool = True) -> str:
    """Format a float as '+2.34%' or '—' if None."""
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:+.2f}%" if signed else f"{f:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _ratio_pct(v: Any) -> str:
    """Format a 0–1 ratio as '62.5%' or '—'."""
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


# ── Core analytics ────────────────────────────────────────────────────────────

def compute_mode_stats(trades: list[dict]) -> dict:
    """Compute all performance metrics for a single mode's closed-trade list.

    Parameters
    ----------
    trades:
        List of closed-trade dicts for one mode only (pre-filtered by caller).
        All fields may be ``None`` on older rows; handled gracefully.

    Returns
    -------
    dict with keys:
        total_trades, win_rate, avg_realized_pnl, avg_mfe, avg_mae,
        avg_giveback, avg_capture_ratio, mfe_sample_count,
        protected_profit_count, protected_profit_rate,
        exit_reason_dist, avg_duration_secs, avg_duration_str
    """
    n = len(trades)

    if n == 0:
        return {
            "total_trades":          0,
            "win_rate":              0.0,
            "avg_realized_pnl":      0.0,
            "avg_mfe":               0.0,
            "avg_mae":               0.0,
            "avg_giveback":          0.0,
            "avg_capture_ratio":     0.0,
            "mfe_sample_count":      0,
            "protected_profit_count": 0,
            "protected_profit_rate": 0.0,
            "exit_reason_dist":      {r: 0 for r in _ALL_REASONS},
            "avg_duration_secs":     None,
            "avg_duration_str":      "—",
        }

    pnls = [float(t.get("pnl_pct") or 0.0) for t in trades]
    mfes = [float(t.get("max_unrealized_profit") or 0.0) for t in trades]
    maes = [float(t.get("min_unrealized_profit") or 0.0) for t in trades]

    win_rate        = sum(1 for p in pnls if p > 0) / n
    avg_realized    = _safe_mean(pnls)
    avg_mfe         = _safe_mean(mfes)
    avg_mae         = _safe_mean(maes)
    avg_giveback    = avg_mfe - avg_realized

    # Capture ratio only when MFE meaningfully > 0
    cap_pairs = [(p, m) for p, m in zip(pnls, mfes) if m > 0.0]
    avg_capture_ratio = _safe_mean([p / m for p, m in cap_pairs]) if cap_pairs else 0.0
    mfe_sample_count  = len(cap_pairs)

    # Protected-profit
    protected_count = sum(1 for t in trades if t.get("was_protected_profit"))
    protected_rate  = protected_count / n

    # Exit reason distribution
    reason_dist: dict[str, int] = {r: 0 for r in _ALL_REASONS}
    for t in trades:
        r = _normalize_reason(t.get("close_reason"))
        reason_dist[r] = reason_dist.get(r, 0) + 1

    # Average duration
    dur_samples = [
        d for d in (
            _duration_secs(t.get("entry_time"), t.get("exit_time"))
            for t in trades
        )
        if d is not None
    ]
    avg_dur_secs = _safe_mean(dur_samples) if dur_samples else None

    return {
        "total_trades":           n,
        "win_rate":               win_rate,
        "avg_realized_pnl":       avg_realized,
        "avg_mfe":                avg_mfe,
        "avg_mae":                avg_mae,
        "avg_giveback":           avg_giveback,
        "avg_capture_ratio":      avg_capture_ratio,
        "mfe_sample_count":       mfe_sample_count,
        "protected_profit_count": protected_count,
        "protected_profit_rate":  protected_rate,
        "exit_reason_dist":       reason_dist,
        "avg_duration_secs":      avg_dur_secs,
        "avg_duration_str":       _fmt_duration(avg_dur_secs),
    }


def compute_all_modes(trades: list[dict]) -> dict[str, dict]:
    """Compute per-mode stats, only counting closed trades, in fixed mode order.

    Parameters
    ----------
    trades:
        All closed-trade dicts (any mix of modes).

    Returns
    -------
    dict mapping each mode label to its stats dict.
    """
    result: dict[str, dict] = {}
    for mode in _MODES:
        mode_trades = [
            t for t in trades
            if (t.get("strategy_mode") or "UNKNOWN") == mode
        ]
        result[mode] = compute_mode_stats(mode_trades)
    return result


def compute_conclusions(by_mode: dict[str, dict]) -> dict:
    """Derive best mode, most-leakage mode, and worst-efficiency mode.

    Rules
    -----
    * **best_mode**: modes with ≥ 5 trades ranked by win_rate desc, then
      avg_realized_pnl desc.
    * **most_leakage_mode**: modes where avg_mfe > 0.1 and ≥ 5 trades, ranked
      by avg_giveback desc.
    * **worst_exit_efficiency_mode**: modes with mfe_sample_count ≥ 5, ranked
      by avg_capture_ratio asc.

    Mode entry is ``None`` when no qualifying mode exists.
    """
    def _qual(mode_stats: dict, min_trades: int = _MIN_TRADES_FOR_CONCLUSION) -> bool:
        return mode_stats.get("total_trades", 0) >= min_trades

    # ── Best performing mode ───────────────────────────────────────────────────
    candidates = [
        (mode, s) for mode, s in by_mode.items() if _qual(s)
    ]
    candidates.sort(
        key=lambda x: (x[1]["win_rate"], x[1]["avg_realized_pnl"]),
        reverse=True,
    )
    if candidates:
        best_mode, best_s = candidates[0]
        best = {
            "mode":             best_mode,
            "win_rate":         best_s["win_rate"],
            "avg_realized_pnl": best_s["avg_realized_pnl"],
            "total_trades":     best_s["total_trades"],
            "reason":           (
                f"Highest win rate {best_s['win_rate']*100:.1f}% "
                f"(avg PnL {best_s['avg_realized_pnl']:+.2f}%)"
            ),
        }
    else:
        best = None

    # ── Most profit leakage (highest avg_mfe − avg_realized_pnl) ──────────────
    leak_candidates = [
        (mode, s) for mode, s in by_mode.items()
        if _qual(s) and s.get("avg_mfe", 0.0) > 0.1
    ]
    leak_candidates.sort(
        key=lambda x: x[1]["avg_giveback"],
        reverse=True,
    )
    if leak_candidates:
        leak_mode, leak_s = leak_candidates[0]
        leakage = {
            "mode":              leak_mode,
            "avg_giveback":      leak_s["avg_giveback"],
            "avg_capture_ratio": leak_s["avg_capture_ratio"],
            "avg_mfe":           leak_s["avg_mfe"],
            "avg_realized_pnl":  leak_s["avg_realized_pnl"],
            "reason":            (
                f"Highest avg giveback {leak_s['avg_giveback']:+.2f}%pts "
                f"from MFE (capture {leak_s['avg_capture_ratio']*100:.1f}%)"
            ),
        }
    else:
        leakage = None

    # ── Worst exit efficiency (lowest capture ratio, enough MFE samples) ───────
    eff_candidates = [
        (mode, s) for mode, s in by_mode.items()
        if s.get("mfe_sample_count", 0) >= _MIN_TRADES_FOR_CONCLUSION
    ]
    eff_candidates.sort(key=lambda x: x[1]["avg_capture_ratio"])
    if eff_candidates:
        eff_mode, eff_s = eff_candidates[0]
        efficiency = {
            "mode":              eff_mode,
            "avg_capture_ratio": eff_s["avg_capture_ratio"],
            "avg_giveback":      eff_s["avg_giveback"],
            "mfe_sample_count":  eff_s["mfe_sample_count"],
            "reason":            (
                f"Lowest capture ratio {eff_s['avg_capture_ratio']*100:.1f}% "
                f"(n={eff_s['mfe_sample_count']} trades with MFE > 0)"
            ),
        }
    else:
        efficiency = None

    return {
        "best_mode":                  best,
        "most_leakage_mode":          leakage,
        "worst_exit_efficiency_mode": efficiency,
    }


# ── Public data-access entry point ────────────────────────────────────────────

def get_mode_performance_data(
    db_path: Any = None,
    limit:   int = 10_000,
) -> dict:
    """Pull closed trades and compute all mode metrics.

    Parameters
    ----------
    db_path:
        SQLite path.  Defaults to ``src.config.SQLITE_PATH``.
    limit:
        Maximum rows to pull (default 10 000 — enough for production use).

    Returns
    -------
    ``{"by_mode": dict, "conclusions": dict, "total_closed": int,
       "generated_at": str}``
    """
    if db_path is not None:
        # Allow callers to pass a custom path for tests; temporarily patch the
        # module-level SQLITE_PATH used by _sqlite_conn()
        import src.data.db as _db_mod
        _orig = _db_mod.SQLITE_PATH
        _db_mod.SQLITE_PATH = str(db_path)
        try:
            trades = _db_mod.get_closed_trades(limit=limit)
        finally:
            _db_mod.SQLITE_PATH = _orig
    else:
        from src.data.db import get_closed_trades
        trades = get_closed_trades(limit=limit)

    by_mode     = compute_all_modes(trades)
    conclusions = compute_conclusions(by_mode)

    return {
        "by_mode":      by_mode,
        "conclusions":  conclusions,
        "total_closed": sum(s["total_trades"] for s in by_mode.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Terminal output ───────────────────────────────────────────────────────────

def print_mode_performance_report(data: dict, *, console=None) -> None:
    """Render the full mode-performance report to the terminal using Rich.

    Outputs three sections:
    1. Core metrics table (one row per mode)
    2. Exit-reason distribution table
    3. Conclusions panel (best / most-leakage / worst-efficiency)

    Falls back to plain text if Rich is not installed.
    """
    try:
        from rich import box as rich_box
        from rich.console import Console
        from rich.panel   import Panel
        from rich.table   import Table
        from rich.text    import Text
    except ImportError:
        _print_plain(data)
        return

    con = console or Console()
    by_mode    = data.get("by_mode",     {})
    conc       = data.get("conclusions", {})
    total      = data.get("total_closed", 0)
    gen_at     = data.get("generated_at", "")[:19].replace("T", " ")

    # ── Table 1: Core metrics ─────────────────────────────────────────────────
    t1 = Table(
        title=f"Strategy Mode Performance  |  {total} closed trades  |  {gen_at}",
        box=rich_box.HEAVY_HEAD,
        style="white on grey7",
        header_style="bold bright_cyan on grey15",
        show_lines=False,
        expand=False,
    )
    t1.add_column("Mode",       style="bold white",   width=14)
    t1.add_column("Trades",     justify="right",      width=7)
    t1.add_column("Win%",       justify="right",      width=7)
    t1.add_column("Avg PnL%",   justify="right",      width=10)
    t1.add_column("Avg MFE%",   justify="right",      width=10)
    t1.add_column("Avg MAE%",   justify="right",      width=10)
    t1.add_column("Giveback%",  justify="right",      width=11)
    t1.add_column("Capture%",   justify="right",      width=10)
    t1.add_column("Protected",  justify="right",      width=10)
    t1.add_column("Avg Dur",    justify="right",      width=9)

    for mode in _MODES:
        s      = by_mode.get(mode, compute_mode_stats([]))
        n      = s["total_trades"]
        pnl_v  = s["avg_realized_pnl"]
        give_v = s["avg_giveback"]
        cap_v  = s["avg_capture_ratio"]

        pnl_s  = Text(f"{pnl_v:+.2f}%",  style="green" if pnl_v  >= 0 else "red")
        give_s = Text(f"{give_v:+.2f}%",  style="red"   if give_v >= 0 else "green")
        cap_s  = Text(
            f"{cap_v*100:.1f}%",
            style="green" if cap_v >= 0.65 else ("yellow" if cap_v >= 0.45 else "red"),
        )
        t1.add_row(
            mode,
            str(n),
            f"{s['win_rate']*100:.1f}%",
            pnl_s,
            f"{s['avg_mfe']:+.2f}%",
            f"{s['avg_mae']:+.2f}%",
            give_s,
            cap_s,
            f"{s['protected_profit_count']} ({s['protected_profit_rate']*100:.0f}%)",
            s["avg_duration_str"],
        )
    con.print(t1)

    # ── Table 2: Exit-reason distribution ────────────────────────────────────
    reason_keys = [k for k in _ALL_REASONS if any(
        by_mode.get(m, {}).get("exit_reason_dist", {}).get(k, 0) > 0
        for m in _MODES
    )]
    if reason_keys:
        t2 = Table(
            title="Exit Reason Distribution",
            box=rich_box.HEAVY_HEAD,
            style="white on grey7",
            header_style="bold bright_cyan on grey15",
            show_lines=False,
            expand=False,
        )
        t2.add_column("Mode", style="bold white", width=14)
        for rk in reason_keys:
            t2.add_column(_REASON_DISPLAY.get(rk, rk), justify="right", width=14)

        for mode in _MODES:
            s    = by_mode.get(mode, compute_mode_stats([]))
            dist = s.get("exit_reason_dist", {})
            t2.add_row(mode, *[str(dist.get(rk, 0)) for rk in reason_keys])
        con.print(t2)

    # ── Conclusion panel ──────────────────────────────────────────────────────
    best  = conc.get("best_mode")
    leak  = conc.get("most_leakage_mode")
    worst = conc.get("worst_exit_efficiency_mode")

    lines: list[str] = []
    if best:
        lines.append(
            f"[bold green]Best Mode:[/bold green]               "
            f"[bold]{best['mode']}[/bold]  —  {best['reason']}"
        )
    else:
        lines.append("[dim]Best Mode: insufficient data[/dim]")

    if leak:
        lines.append(
            f"[bold red]Most Profit Leakage:[/bold red]     "
            f"[bold]{leak['mode']}[/bold]  —  {leak['reason']}"
        )
    else:
        lines.append("[dim]Most Profit Leakage: insufficient data[/dim]")

    if worst:
        lines.append(
            f"[bold yellow]Worst Exit Efficiency:[/bold yellow]   "
            f"[bold]{worst['mode']}[/bold]  —  {worst['reason']}"
        )
    else:
        lines.append("[dim]Worst Exit Efficiency: insufficient data[/dim]")

    con.print(Panel("\n".join(lines), title="[bold]Conclusions[/bold]", expand=False))


def _print_plain(data: dict) -> None:
    """Fallback terminal output when Rich is not installed."""
    by_mode = data.get("by_mode",     {})
    conc    = data.get("conclusions", {})
    print(f"=== Strategy Mode Performance Report ({data.get('total_closed',0)} closed) ===")
    hdr = f"{'Mode':<14} {'Trades':>7} {'Win%':>7} {'Avg PnL%':>10} {'Avg MFE%':>10} {'Giveback%':>11} {'Capture%':>10}"
    print(hdr)
    for mode in _MODES:
        s = by_mode.get(mode, compute_mode_stats([]))
        print(
            f"{mode:<14}"
            f" {s['total_trades']:>7}"
            f" {s['win_rate']*100:>6.1f}%"
            f" {s['avg_realized_pnl']:>+10.2f}%"
            f" {s['avg_mfe']:>+10.2f}%"
            f" {s['avg_giveback']:>+11.2f}%"
            f" {s['avg_capture_ratio']*100:>9.1f}%"
        )
    print("\n--- Exit Reasons ---")
    for mode in _MODES:
        s    = by_mode.get(mode, compute_mode_stats([]))
        dist = s.get("exit_reason_dist", {})
        print(f"  {mode}: " + " | ".join(f"{k}={v}" for k, v in dist.items() if v))
    print("\n--- Conclusions ---")
    for key in ("best_mode", "most_leakage_mode", "worst_exit_efficiency_mode"):
        c = conc.get(key)
        label = key.replace("_", " ").title()
        if c:
            print(f"  {label}: {c['mode']}  — {c['reason']}")
        else:
            print(f"  {label}: insufficient data")


# ── Markdown output ───────────────────────────────────────────────────────────

def mode_performance_to_markdown(data: dict) -> str:
    """Return a Markdown-formatted mode performance summary string.

    Safe to copy into issue trackers, operation logs, or documentation.
    Does not write any files.
    """
    by_mode   = data.get("by_mode",     {})
    conc      = data.get("conclusions", {})
    total     = data.get("total_closed", 0)
    gen_at    = data.get("generated_at", "")[:19].replace("T", " ")

    lines: list[str] = [
        "# Strategy Mode Performance Report",
        "",
        f"Generated: {gen_at} UTC  |  Total closed trades: **{total}**",
        "",
    ]

    # ── Core metrics table ────────────────────────────────────────────────────
    lines.append("## Core Metrics by Mode")
    lines.append("")
    lines.append(
        "| Mode | Trades | Win% | Avg PnL% | Avg MFE% | Avg MAE% "
        "| Giveback% | Capture% | Protected | Avg Duration |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for mode in _MODES:
        s = by_mode.get(mode, compute_mode_stats([]))
        lines.append(
            f"| {mode}"
            f" | {s['total_trades']}"
            f" | {s['win_rate']*100:.1f}%"
            f" | {s['avg_realized_pnl']:+.2f}%"
            f" | {s['avg_mfe']:+.2f}%"
            f" | {s['avg_mae']:+.2f}%"
            f" | {s['avg_giveback']:+.2f}%"
            f" | {s['avg_capture_ratio']*100:.1f}%"
            f" | {s['protected_profit_count']} ({s['protected_profit_rate']*100:.0f}%)"
            f" | {s['avg_duration_str']} |"
        )
    lines.append("")

    # ── Exit reason distribution ──────────────────────────────────────────────
    reason_keys = [k for k in _ALL_REASONS if any(
        by_mode.get(m, {}).get("exit_reason_dist", {}).get(k, 0) > 0
        for m in _MODES
    )]
    if reason_keys:
        lines.append("## Exit Reason Distribution")
        lines.append("")
        col_headers = " | ".join(_REASON_DISPLAY.get(rk, rk) for rk in reason_keys)
        lines.append(f"| Mode | {col_headers} |")
        lines.append("|---|" + "---|" * len(reason_keys))
        for mode in _MODES:
            s    = by_mode.get(mode, compute_mode_stats([]))
            dist = s.get("exit_reason_dist", {})
            vals = " | ".join(str(dist.get(rk, 0)) for rk in reason_keys)
            lines.append(f"| {mode} | {vals} |")
        lines.append("")

    # ── Conclusions ───────────────────────────────────────────────────────────
    lines.append("## Conclusions")
    lines.append("")
    lines.append("| Category | Mode | Key Metric |")
    lines.append("|---|---|---|")

    best  = conc.get("best_mode")
    leak  = conc.get("most_leakage_mode")
    worst = conc.get("worst_exit_efficiency_mode")

    if best:
        lines.append(f"| **Best Performance** | {best['mode']} | {best['reason']} |")
    else:
        lines.append("| **Best Performance** | — | Insufficient data |")

    if leak:
        lines.append(f"| **Most Profit Leakage** | {leak['mode']} | {leak['reason']} |")
    else:
        lines.append("| **Most Profit Leakage** | — | Insufficient data |")

    if worst:
        lines.append(f"| **Worst Exit Efficiency** | {worst['mode']} | {worst['reason']} |")
    else:
        lines.append("| **Worst Exit Efficiency** | — | Insufficient data |")

    lines.append("")

    # ── Contextual observation ────────────────────────────────────────────────
    lines.append("## Contextual Observations")
    lines.append("")
    lines.append(
        "> *Known context from operator notes: SCALP has the biggest opportunity "
        "but unstable quality; INTERMEDIATE is mixed; SWING is weakest.*"
    )
    lines.append("")
    for mode in _MODES:
        s = by_mode.get(mode, compute_mode_stats([]))
        if s["total_trades"] == 0:
            lines.append(f"- **{mode}**: no closed trades in dataset")
            continue
        cap = s["avg_capture_ratio"]
        give = s["avg_giveback"]
        obs = []
        if cap < 0.45:
            obs.append(f"capture ratio {cap*100:.1f}% is poor — exits missing much of MFE")
        if give > 1.0:
            obs.append(f"avg giveback {give:+.2f}%pts is high — profit frequently leaking")
        if s["win_rate"] > 0.55:
            obs.append(f"win rate {s['win_rate']*100:.1f}% is above 55% threshold")
        if obs:
            lines.append(f"- **{mode}**: " + "; ".join(obs))
        else:
            lines.append(
                f"- **{mode}**: win rate {s['win_rate']*100:.1f}%, "
                f"avg PnL {s['avg_realized_pnl']:+.2f}%, "
                f"capture {cap*100:.1f}%"
            )

    lines.append("")
    return "\n".join(lines)


# ── JSON output ───────────────────────────────────────────────────────────────

def mode_performance_to_json(data: dict) -> str:
    """Serialise the full mode performance data to a pretty-printed JSON string.

    Returns a stable structure suitable for downstream automation:

    .. code-block:: json

        {
          "generated_at": "...",
          "total_closed": 300,
          "by_mode": {
            "SCALP": {
              "total_trades": 120,
              "win_rate": 0.625,
              "avg_realized_pnl": 1.24,
              "avg_mfe": 2.45,
              "avg_mae": -0.83,
              "avg_giveback": 1.21,
              "avg_capture_ratio": 0.562,
              "mfe_sample_count": 95,
              "protected_profit_count": 45,
              "protected_profit_rate": 0.375,
              "exit_reason_dist": { ... },
              "avg_duration_secs": 8280.0,
              "avg_duration_str": "2h 18m"
            }
          },
          "conclusions": {
            "best_mode": { ... },
            "most_leakage_mode": { ... },
            "worst_exit_efficiency_mode": { ... }
          }
        }
    """
    # Ensure floats are round to 4dp for cleanliness
    def _clean(obj):
        if isinstance(obj, float):
            return round(obj, 4)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        return obj

    return json.dumps(_clean(data), indent=2, default=str)
