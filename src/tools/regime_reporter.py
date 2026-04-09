"""Regime performance report — Phase 11.

Three output formats:
    terminal  — via print_regime_report(data)
    markdown  — via regime_to_markdown(data)
    JSON      — via regime_to_json(data)

Usage::

    from src.tools.regime_reporter import (
        get_regime_report_data,
        print_regime_report,
        regime_to_markdown,
        regime_to_json,
    )

    data = get_regime_report_data()
    print_regime_report(data)
    md   = regime_to_markdown(data)
    js   = regime_to_json(data)

What the report answers
-----------------------
* Which regime labels occur most often by asset and mode
* Which regimes produce the best / worst PnL outcomes
* Which regimes produce the worst leakage (MFE captured vs given back)
* Best / worst regimes per SCALP / INTERMEDIATE / SWING
* Whether thresholds appear too loose or tight in certain regimes
  (signal quality metrics: win rate, avg score, ML/AI pass rate)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_ALL_LABELS = [
    "TRENDING_HIGH_VOL",
    "TRENDING_LOW_VOL",
    "CHOPPY_HIGH_VOL",
    "CHOPPY_LOW_VOL",
    "REVERSAL_TRANSITION",
    "NEWS_DRIVEN_UNSTABLE",
    "UNKNOWN",
]

_MODES = ("SCALP", "INTERMEDIATE", "SWING")

_MIN_TRADES_FOR_RANKING = 3   # minimum trades in a bucket before it qualifies for best/worst


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _safe_mean(vals: list) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _win_rate(trades: list[dict]) -> float:
    wins = [t for t in trades if (t.get("pnl_pct") or 0.0) > 0]
    return round(len(wins) / len(trades), 4) if trades else 0.0


def _avg_pnl(trades: list[dict]) -> float:
    vals = [float(t.get("pnl_pct") or 0.0) for t in trades]
    return _safe_mean(vals)


def _avg_mfe(trades: list[dict]) -> float:
    vals = [float(t.get("max_unrealized_profit") or 0.0) for t in trades]
    return _safe_mean(vals)


def _avg_leakage(trades: list[dict]) -> float:
    """Mean of (MFE − PnL) — how much of the best move was given back."""
    vals = []
    for t in trades:
        mfe = float(t.get("max_unrealized_profit") or 0.0)
        pnl = float(t.get("pnl_pct") or 0.0)
        if mfe > 0:
            vals.append(mfe - pnl)
    return _safe_mean(vals)


def _capture_ratio(trades: list[dict]) -> float:
    """PnL / MFE — fraction of the best move actually captured."""
    vals = []
    for t in trades:
        mfe = float(t.get("max_unrealized_profit") or 0.0)
        pnl = float(t.get("pnl_pct") or 0.0)
        if mfe > 0.01:
            vals.append(min(pnl / mfe, 1.0))
    return _safe_mean(vals)


def _avg_mae(trades: list[dict]) -> float:
    vals = [float(t.get("min_unrealized_profit") or 0.0) for t in trades]
    return _safe_mean(vals)


def _regime_change_rate(trades: list[dict]) -> float:
    """Fraction of trades where regime changed during the trade."""
    if not trades:
        return 0.0
    changed = sum(1 for t in trades if t.get("regime_changed_during_trade"))
    return round(changed / len(trades), 4)


def _compute_bucket_stats(trades: list[dict]) -> dict:
    """Compute all performance metrics for a list of trades."""
    n = len(trades)
    return {
        "total_trades":    n,
        "win_rate":        _win_rate(trades),
        "avg_pnl":         _avg_pnl(trades),
        "avg_mfe":         _avg_mfe(trades),
        "avg_mae":         _avg_mae(trades),
        "avg_leakage":     _avg_leakage(trades),
        "capture_ratio":   _capture_ratio(trades),
        "avg_score_total": _safe_mean([float(t.get("score_total") or 0.0) for t in trades]),
        "regime_change_rate": _regime_change_rate(trades),
    }


# ── Data loading + aggregation ────────────────────────────────────────────────

def _load_trades(db_path: Optional[str] = None, limit: int = 10_000) -> list[dict]:
    """Return closed trades from SQLite, optionally from a custom path."""
    try:
        import src.data.db as db_mod
        if db_path:
            import sqlite3, contextlib
            from pathlib import Path
            p = Path(db_path)
            if not p.exists():
                return []
            with sqlite3.connect(str(p), detect_types=sqlite3.PARSE_DECLTYPES) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status='CLOSED' ORDER BY exit_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        else:
            return db_mod.get_closed_trades(limit=limit)
    except Exception as exc:
        log.warning("_load_trades failed: %s", exc)
        return []


def _load_regime_snapshots(db_path: Optional[str] = None, limit: int = 50_000) -> list[dict]:
    """Return regime snapshots from SQLite."""
    try:
        import src.data.db as db_mod
        if db_path:
            import sqlite3
            from pathlib import Path
            p = Path(db_path)
            if not p.exists():
                return []
            with sqlite3.connect(str(p), detect_types=sqlite3.PARSE_DECLTYPES) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM regime_snapshots ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        else:
            return db_mod.get_regime_snapshots(limit=limit)
    except Exception as exc:
        log.debug("_load_regime_snapshots failed: %s", exc)
        return []


def _group_trades_by(trades: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        v = str(t.get(key) or "UNKNOWN")
        groups.setdefault(v, []).append(t)
    return groups


def _snapshot_distribution(snapshots: list[dict]) -> dict[str, dict]:
    """Count snapshot occurrences per regime label."""
    counts: dict[str, int] = {}
    for s in snapshots:
        label = s.get("regime_label") or "UNKNOWN"
        counts[label] = counts.get(label, 0) + 1
    total = max(sum(counts.values()), 1)
    return {
        label: {"count": cnt, "pct": round(cnt / total * 100, 1)}
        for label, cnt in sorted(counts.items(), key=lambda x: -x[1])
    }


def _threshold_diagnostics(trades: list[dict]) -> dict[str, Any]:
    """Simple signal-quality diagnostics per regime label.

    Answers: which regimes accept too many low-confidence signals
    or too many high-score-but-losing trades?
    """
    by_regime = _group_trades_by(trades, "regime_label_at_entry")
    result: dict[str, dict] = {}
    for label, t_list in by_regime.items():
        if not t_list:
            continue
        win_rate  = _win_rate(t_list)
        avg_score = _safe_mean([float(t.get("score_total") or 0.0) for t in t_list])
        avg_ml    = _safe_mean([float(t.get("ml_confidence") or 0.0) for t in t_list])
        avg_ai    = _safe_mean([float(t.get("ai_confidence") or 0.0) for t in t_list])
        # Heuristic flag: high average score but below-50% win rate suggests loose thresholds
        threshold_concern = (avg_score >= 60.0 and win_rate < 0.50 and len(t_list) >= _MIN_TRADES_FOR_RANKING)
        result[label] = {
            "total_trades":    len(t_list),
            "win_rate":        win_rate,
            "avg_score_total": avg_score,
            "avg_ml_conf":     avg_ml,
            "avg_ai_conf":     avg_ai,
            "threshold_concern": threshold_concern,
        }
    return result


def _transition_stability(trades: list[dict]) -> dict[str, Any]:
    """Phase 12: Compute regime transition stability metrics."""
    if not trades:
        return {"total_trades": 0, "trades_with_regime_change": 0, "change_rate": 0.0}

    changed = [t for t in trades if t.get("regime_changed_during_trade")]
    change_rate = round(len(changed) / len(trades), 4) if trades else 0.0

    # PnL comparison: trades with vs without regime changes
    stable_trades = [t for t in trades if not t.get("regime_changed_during_trade")]
    return {
        "total_trades":               len(trades),
        "trades_with_regime_change":  len(changed),
        "change_rate":                change_rate,
        "avg_pnl_stable":            _avg_pnl(stable_trades),
        "avg_pnl_changed":           _avg_pnl(changed),
        "win_rate_stable":           _win_rate(stable_trades),
        "win_rate_changed":          _win_rate(changed),
    }


# ── Main data computation ─────────────────────────────────────────────────────

def get_regime_report_data(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> dict:
    """Load closed trades and regime snapshots; compute all regime metrics.

    Returns a dict compatible with all three format functions.
    """
    trades    = _load_trades(db_path=db_path, limit=limit)
    snapshots = _load_regime_snapshots(db_path=db_path)

    by_regime        = _group_trades_by(trades, "regime_label_at_entry")
    by_mode          = _group_trades_by(trades, "strategy_mode")
    snap_distribution = _snapshot_distribution(snapshots)
    threshold_diags  = _threshold_diagnostics(trades)

    # Per-regime outcome stats
    regime_stats: dict[str, dict] = {}
    for label in _ALL_LABELS:
        t_list = by_regime.get(label, [])
        regime_stats[label] = _compute_bucket_stats(t_list)

    # Per-mode × per-regime cross-tab
    mode_regime_stats: dict[str, dict[str, dict]] = {}
    for mode in _MODES:
        mode_trades = by_mode.get(mode, [])
        mode_by_regime = _group_trades_by(mode_trades, "regime_label_at_entry")
        mode_regime_stats[mode] = {}
        for label in _ALL_LABELS:
            t_list = mode_by_regime.get(label, [])
            mode_regime_stats[mode][label] = _compute_bucket_stats(t_list)

    # Best / worst regimes (min trades gate)
    qualified = [
        (lbl, stats)
        for lbl, stats in regime_stats.items()
        if stats["total_trades"] >= _MIN_TRADES_FOR_RANKING and lbl != "UNKNOWN"
    ]
    best_regime_pnl     = max(qualified, key=lambda x: x[1]["avg_pnl"],     default=(None, {}))[0]
    worst_regime_pnl    = min(qualified, key=lambda x: x[1]["avg_pnl"],     default=(None, {}))[0]
    most_leakage_regime = max(qualified, key=lambda x: x[1]["avg_leakage"], default=(None, {}))[0]
    best_capture_regime = max(
        [(l, s) for l, s in qualified if s["avg_mfe"] > 0.01],
        key=lambda x: x[1]["capture_ratio"], default=(None, {})
    )[0]

    # Best / worst regime per mode
    best_regime_per_mode:  dict[str, Optional[str]] = {}
    worst_regime_per_mode: dict[str, Optional[str]] = {}
    for mode in _MODES:
        mode_q = [
            (lbl, stats)
            for lbl, stats in mode_regime_stats[mode].items()
            if stats["total_trades"] >= _MIN_TRADES_FOR_RANKING and lbl != "UNKNOWN"
        ]
        best_regime_per_mode[mode]  = max(mode_q, key=lambda x: x[1]["avg_pnl"], default=(None, {}))[0]
        worst_regime_per_mode[mode] = min(mode_q, key=lambda x: x[1]["avg_pnl"], default=(None, {}))[0]

    return {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "total_closed_trades":   len(trades),
        "total_regime_snapshots":len(snapshots),
        "regime_stats":          regime_stats,
        "mode_regime_stats":     mode_regime_stats,
        "snapshot_distribution": snap_distribution,
        "threshold_diagnostics": threshold_diags,
        "transition_stability":  _transition_stability(trades),
        "conclusions": {
            "best_regime_by_pnl":       best_regime_pnl,
            "worst_regime_by_pnl":      worst_regime_pnl,
            "most_leakage_regime":      most_leakage_regime,
            "best_capture_regime":      best_capture_regime,
            "best_regime_per_mode":     best_regime_per_mode,
            "worst_regime_per_mode":    worst_regime_per_mode,
        },
    }


# ── Terminal output ───────────────────────────────────────────────────────────

def print_regime_report(data: dict, *, console: Any = None) -> None:
    """Print an ASCII | Rich regime performance report to the terminal."""
    try:
        from rich.console import Console
        from rich.table   import Table
        from rich.panel   import Panel
        con = console or Console()
        _print_rich(data, con)
    except ImportError:
        _print_plain(data)


def _print_rich(data: dict, con: Any) -> None:
    from rich.table import Table
    from rich.panel import Panel

    con.print(Panel(
        f"[bold]Regime Performance Report[/bold]  "
        f"({data.get('total_closed_trades', 0)} trades | "
        f"{data.get('total_regime_snapshots', 0)} snapshots | "
        f"generated {data.get('generated_at', '')})",
        style="bold blue",
    ))

    # Regime stats table
    t = Table(title="Regime outcomes", show_lines=True)
    t.add_column("Regime",          style="cyan",   min_width=22)
    t.add_column("Trades",          justify="right")
    t.add_column("Win%",            justify="right")
    t.add_column("Avg PnL%",        justify="right")
    t.add_column("Avg MFE%",        justify="right")
    t.add_column("Avg MAE%",        justify="right")
    t.add_column("Avg Leakage%",    justify="right")
    t.add_column("Capture",         justify="right")
    t.add_column("Avg Score",       justify="right")

    for label, stats in data.get("regime_stats", {}).items():
        n = stats.get("total_trades", 0)
        if n == 0:
            continue
        t.add_row(
            label,
            str(n),
            f"{stats['win_rate']:.0%}",
            f"{stats['avg_pnl']:.2f}%",
            f"{stats['avg_mfe']:.2f}%",
            f"{stats.get('avg_mae', 0):.2f}%",
            f"{stats['avg_leakage']:.2f}%",
            f"{stats['capture_ratio']:.2f}",
            f"{stats['avg_score_total']:.1f}",
        )
    con.print(t)

    # Snapshot frequency
    snap_dist = data.get("snapshot_distribution", {})
    if snap_dist:
        t2 = Table(title="Regime frequency (snapshots)", show_lines=False)
        t2.add_column("Label",  style="cyan")
        t2.add_column("Count",  justify="right")
        t2.add_column("Pct",    justify="right")
        for label, info in snap_dist.items():
            t2.add_row(label, str(info.get("count", 0)), f"{info.get('pct', 0):.1f}%")
        con.print(t2)

    # Conclusions
    conc = data.get("conclusions", {})
    lines = [
        f"Best regime by PnL:     {conc.get('best_regime_by_pnl') or 'n/a'}",
        f"Worst regime by PnL:    {conc.get('worst_regime_by_pnl') or 'n/a'}",
        f"Most leakage regime:    {conc.get('most_leakage_regime') or 'n/a'}",
        f"Best capture regime:    {conc.get('best_capture_regime') or 'n/a'}",
    ]
    for mode in _MODES:
        best  = (conc.get("best_regime_per_mode")  or {}).get(mode) or "n/a"
        worst = (conc.get("worst_regime_per_mode") or {}).get(mode) or "n/a"
        lines.append(f"{mode:14s}: best={best}  worst={worst}")
    con.print(Panel("\n".join(lines), title="Conclusions", style="green"))

    # Threshold diagnostics
    diags = data.get("threshold_diagnostics", {})
    concerns = [(lbl, d) for lbl, d in diags.items() if d.get("threshold_concern")]
    if concerns:
        con.print("[yellow]⚠  Threshold concerns (high score + low win rate):[/yellow]")
        for lbl, d in concerns:
            con.print(
                f"  {lbl}: score={d['avg_score_total']:.1f} "
                f"win={d['win_rate']:.0%} "
                f"trades={d['total_trades']}"
            )

    # Phase 12: Transition stability
    ts = data.get("transition_stability", {})
    if ts.get("total_trades", 0) > 0:
        ts_lines = [
            f"Total trades:            {ts.get('total_trades', 0)}",
            f"Trades with regime flip: {ts.get('trades_with_regime_change', 0)} "
            f"({ts.get('change_rate', 0):.0%})",
            f"Avg PnL (stable):        {ts.get('avg_pnl_stable', 0):.2f}%",
            f"Avg PnL (changed):       {ts.get('avg_pnl_changed', 0):.2f}%",
            f"Win rate (stable):       {ts.get('win_rate_stable', 0):.0%}",
            f"Win rate (changed):      {ts.get('win_rate_changed', 0):.0%}",
        ]
        con.print(Panel("\n".join(ts_lines), title="Transition Stability", style="magenta"))


def _print_plain(data: dict) -> None:
    print("=" * 70)
    print("REGIME PERFORMANCE REPORT")
    print(f"Generated: {data.get('generated_at', '')}")
    print(f"Trades: {data.get('total_closed_trades', 0)}  "
          f"Snapshots: {data.get('total_regime_snapshots', 0)}")
    print("-" * 70)
    print(f"{'Regime':<24} {'N':>5} {'Win%':>7} {'Avg PnL':>9} "
          f"{'Avg MFE':>9} {'Leakage':>9} {'Capt':>7}")
    for label, stats in data.get("regime_stats", {}).items():
        n = stats.get("total_trades", 0)
        if n == 0:
            continue
        print(
            f"{label:<24} {n:>5} {stats['win_rate']:>7.0%} "
            f"{stats['avg_pnl']:>9.2f} {stats['avg_mfe']:>9.2f} "
            f"{stats['avg_leakage']:>9.2f} {stats['capture_ratio']:>7.2f}"
        )
    conc = data.get("conclusions", {})
    print("-" * 70)
    print(f"Best by PnL:  {conc.get('best_regime_by_pnl') or 'n/a'}")
    print(f"Worst by PnL: {conc.get('worst_regime_by_pnl') or 'n/a'}")
    print(f"Most leakage: {conc.get('most_leakage_regime') or 'n/a'}")
    for mode in _MODES:
        best  = (conc.get("best_regime_per_mode")  or {}).get(mode) or "n/a"
        worst = (conc.get("worst_regime_per_mode") or {}).get(mode) or "n/a"
        print(f"{mode}: best={best}  worst={worst}")
    print("=" * 70)


# ── Markdown export ───────────────────────────────────────────────────────────

def regime_to_markdown(data: dict) -> str:
    """Return the regime report as a markdown string (no file I/O)."""
    lines: list[str] = [
        "# Regime Performance Report",
        "",
        f"**Generated:** {data.get('generated_at', '')}  ",
        f"**Closed trades:** {data.get('total_closed_trades', 0)}  ",
        f"**Regime snapshots:** {data.get('total_regime_snapshots', 0)}",
        "",
        "## Regime Outcome Stats",
        "",
        "| Regime | Trades | Win% | Avg PnL% | Avg MFE% | Avg MAE% | Leakage% | Capture | Avg Score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, stats in data.get("regime_stats", {}).items():
        n = stats.get("total_trades", 0)
        if n == 0:
            continue
        lines.append(
            f"| {label} | {n} | {stats['win_rate']:.0%} | "
            f"{stats['avg_pnl']:.2f} | {stats['avg_mfe']:.2f} | "
            f"{stats.get('avg_mae', 0):.2f} | "
            f"{stats['avg_leakage']:.2f} | {stats['capture_ratio']:.2f} | "
            f"{stats['avg_score_total']:.1f} |"
        )

    lines += ["", "## Mode × Regime Cross-Tab", ""]
    for mode in _MODES:
        lines += [f"### {mode}", ""]
        lines += [
            "| Regime | Trades | Win% | Avg PnL% | Capture |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        mode_stats = data.get("mode_regime_stats", {}).get(mode, {})
        for label, stats in mode_stats.items():
            n = stats.get("total_trades", 0)
            if n == 0:
                continue
            lines.append(
                f"| {label} | {n} | {stats['win_rate']:.0%} | "
                f"{stats['avg_pnl']:.2f} | {stats['capture_ratio']:.2f} |"
            )
        lines.append("")

    # Regime frequency
    snap_dist = data.get("snapshot_distribution", {})
    if snap_dist:
        lines += ["## Regime Frequency (Snapshots)", ""]
        lines += ["| Label | Count | Pct |", "| --- | ---: | ---: |"]
        for label, info in snap_dist.items():
            lines.append(f"| {label} | {info.get('count', 0)} | {info.get('pct', 0):.1f}% |")
        lines.append("")

    # Conclusions
    conc = data.get("conclusions", {})
    lines += [
        "## Conclusions",
        "",
        f"- **Best regime by PnL:** {conc.get('best_regime_by_pnl') or 'n/a'}",
        f"- **Worst regime by PnL:** {conc.get('worst_regime_by_pnl') or 'n/a'}",
        f"- **Most leakage:** {conc.get('most_leakage_regime') or 'n/a'}",
        f"- **Best capture:** {conc.get('best_capture_regime') or 'n/a'}",
    ]
    for mode in _MODES:
        best  = (conc.get("best_regime_per_mode")  or {}).get(mode) or "n/a"
        worst = (conc.get("worst_regime_per_mode") or {}).get(mode) or "n/a"
        lines.append(f"- **{mode}:** best={best}  worst={worst}")

    # Threshold diagnostics
    diags = data.get("threshold_diagnostics", {})
    concerns = [(lbl, d) for lbl, d in diags.items() if d.get("threshold_concern")]
    if concerns:
        lines += ["", "## Threshold Concerns", ""]
        lines += ["| Regime | Trades | Win% | Avg Score |", "| --- | ---: | ---: | ---: |"]
        for lbl, d in concerns:
            lines.append(
                f"| {lbl} | {d['total_trades']} | "
                f"{d['win_rate']:.0%} | {d['avg_score_total']:.1f} |"
            )

    # Phase 12: Transition stability
    ts = data.get("transition_stability", {})
    if ts.get("total_trades", 0) > 0:
        lines += [
            "", "## Transition Stability", "",
            f"- **Trades with regime change:** {ts.get('trades_with_regime_change', 0)} "
            f"/ {ts.get('total_trades', 0)} ({ts.get('change_rate', 0):.0%})",
            f"- **Avg PnL (stable):** {ts.get('avg_pnl_stable', 0):.2f}%",
            f"- **Avg PnL (changed):** {ts.get('avg_pnl_changed', 0):.2f}%",
            f"- **Win rate (stable):** {ts.get('win_rate_stable', 0):.0%}",
            f"- **Win rate (changed):** {ts.get('win_rate_changed', 0):.0%}",
        ]

    return "\n".join(lines)


# ── JSON export ───────────────────────────────────────────────────────────────

def regime_to_json(data: dict) -> str:
    """Return the regime report as a pretty-printed JSON string."""
    def _clean(obj: Any) -> Any:
        if isinstance(obj, float):
            return round(obj, 4)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        return obj
    return json.dumps(_clean(data), indent=2, default=str)
