"""Backtest reporter — formats trade ledger and performance metrics using Rich.

Called after Backtester.run() returns the list of TradeRecord objects.
Outputs:
  • Full trade-by-trade ledger table
  • Summary statistics panel (win rate, profit factor, max drawdown, Sharpe)
  • Optionally writes CSV to data/backtest_SYMBOL_TIMEFRAME_DATE.csv
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from rich import box
from rich.console import Console
from rich.panel   import Panel
from rich.table   import Table
from rich.text    import Text

log     = logging.getLogger(__name__)
console = Console()

try:
    from src.signals.types import TradeRecord
    from src.config        import DATA_DIR, TIMEZONE
except ImportError:
    from pathlib import Path
    DATA_DIR = Path("data")
    TIMEZONE = "America/Toronto"

try:
    import pytz
    _tz = pytz.timezone(TIMEZONE)
except Exception:
    _tz = timezone.utc


def _fmt_ts(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_tz)
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _duration(entry: Optional[datetime], exit_: Optional[datetime]) -> str:
    if entry is None or exit_ is None:
        return "—"
    delta = abs(exit_ - entry)
    hours = int(delta.total_seconds() // 3600)
    mins  = int((delta.total_seconds() % 3600) // 60)
    return f"{hours}h {mins}m"


# ── Close-reason normalisation ───────────────────────────────────────────────

# Canonical display labels (used in ledger and CSV-readable columns).
_REASON_DISPLAY: dict[str, str] = {
    "PEAK_GIVEBACK_EXIT": "Peak Giveback Exit",
    "TRAILING_TP":        "Peak Giveback Exit",   # legacy alias — pre-Phase-2 rows
    "HARD_STOP":          "Hard Stop",
    "TRAIL_STOP":         "Trailing Stop",
    "ALLIGATOR_TP":       "Alligator TP",
    "MANUAL":             "Manual",
}


def _normalize_reason(raw: str) -> str:
    """Collapse legacy TRAILING_TP into PEAK_GIVEBACK_EXIT for aggregation.

    Ensures backtest metrics bucket both old and new rows under the same key
    even if the DB migration has not yet run (e.g. in-memory backtests).
    """
    return "PEAK_GIVEBACK_EXIT" if raw == "TRAILING_TP" else raw


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(trades: list[TradeRecord]) -> dict:
    closed = [t for t in trades if t.status == "CLOSED"]
    if not closed:
        return {}

    pnls     = [t.pnl_pct for t in closed]
    winners  = [p for p in pnls if p > 0]
    losers   = [p for p in pnls if p <= 0]

    win_rate    = len(winners) / len(closed)
    avg_win     = np.mean(winners)  if winners else 0.0
    avg_loss    = np.mean(losers)   if losers  else 0.0
    total_pnl   = sum(pnls)
    gross_profit= sum(winners)      if winners else 0.0
    gross_loss  = abs(sum(losers))  if losers  else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown (sequential equity curve)
    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    # Simplified Sharpe (daily return approximation)
    if len(pnls) > 1:
        sharpe = np.mean(pnls) / np.std(pnls) * math.sqrt(252) if np.std(pnls) > 0 else float("nan")
    else:
        sharpe = float("nan")

    by_reason: dict[str, int] = {}
    for t in closed:
        r = _normalize_reason(t.close_reason or "UNKNOWN")
        by_reason[r] = by_reason.get(r, 0) + 1

    # Per-mode breakdown (excludes UNKNOWN)
    by_mode: dict[str, dict] = {}
    for mode_label in ("SCALP", "INTERMEDIATE", "SWING"):
        mode_trades = [t for t in closed if getattr(t, "strategy_mode", "UNKNOWN") == mode_label]
        if not mode_trades:
            continue
        m_pnls = [t.pnl_pct for t in mode_trades]
        m_wins = [p for p in m_pnls if p > 0]
        by_mode[mode_label] = {
            "count":     len(mode_trades),
            "winners":   len(m_wins),
            "win_rate":  len(m_wins) / len(mode_trades),
            "total_pnl": sum(m_pnls),
            "avg_pnl":   float(np.mean(m_pnls)),
            "close_reasons": {},
        }
        for t in mode_trades:
            r = _normalize_reason(t.close_reason or "UNKNOWN")
            by_mode[mode_label]["close_reasons"][r] = by_mode[mode_label]["close_reasons"].get(r, 0) + 1

    return {
        "total_trades":   len(closed),
        "winners":        len(winners),
        "losers":         len(losers),
        "win_rate":       win_rate,
        "avg_win_pct":    avg_win,
        "avg_loss_pct":   avg_loss,
        "total_pnl_pct":  total_pnl,
        "profit_factor":  profit_factor,
        "max_drawdown":   max_dd,
        "sharpe":         sharpe,
        "close_reasons":  by_reason,
        "by_mode":        by_mode,
    }


# ── Rich output ───────────────────────────────────────────────────────────────

def print_trade_ledger(trades: list[TradeRecord]) -> None:
    """Print every closed trade in a rich ledger table."""
    closed = [t for t in trades if t.status == "CLOSED"]
    if not closed:
        console.print("[yellow]No closed trades to display.[/]")
        return

    tbl = Table(
        title="📋  Backtest Trade Ledger",
        box=box.HEAVY_HEAD,
        show_lines=True,
        style="white on grey7",
    )
    tbl.add_column("#",          style="dim",      width=4,  justify="right")
    tbl.add_column("Type",       style="bold",     width=5)
    tbl.add_column("Asset",      style="cyan",     width=10)
    tbl.add_column("TF",         style="dim",      width=5)
    tbl.add_column("Mode",       style="white",    width=13)
    tbl.add_column("Entry Time", style="white",    width=17)
    tbl.add_column("Exit Time",  style="white",    width=17)
    tbl.add_column("Duration",   style="dim",      width=10)
    tbl.add_column("Entry",      style="white",    width=12, justify="right")
    tbl.add_column("Exit",       style="white",    width=12, justify="right")
    tbl.add_column("PnL %",      style="bold",     width=10, justify="right")
    tbl.add_column("Reason",     style="dim",      width=14)

    for i, t in enumerate(closed, 1):
        pnl_str = f"{t.pnl_pct:+.2f}%"
        pnl_col = "[green]" + pnl_str + "[/]" if t.pnl_pct > 0 else "[red]" + pnl_str + "[/]"
        type_col = "[green]BUY[/]" if t.signal_type == "BUY" else "[red]SELL[/]"
        reason = _REASON_DISPLAY.get(
            t.close_reason or "",
            (t.close_reason or "").replace("_", " ").title(),
        )
        tbl.add_row(
            str(i),
            type_col,
            t.asset,
            t.timeframe,
            getattr(t, "strategy_mode", "UNKNOWN"),
            _fmt_ts(t.entry_time),
            _fmt_ts(t.exit_time),
            _duration(t.entry_time, t.exit_time),
            f"{t.entry_price:.5f}",
            f"{t.exit_price:.5f}" if t.exit_price else "—",
            pnl_col,
            reason,
        )
    console.print(tbl)


def print_backtest_summary(trades: list[TradeRecord], symbol: str, timeframe: str) -> None:
    """Print performance summary panel."""
    m = _compute_metrics(trades)
    if not m:
        console.print("[yellow]No trades completed in this backtest.[/]")
        return

    pf_str = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "∞"
    sharpe_str = f"{m['sharpe']:.2f}" if not math.isnan(m["sharpe"]) else "N/A"
    reasons_str = "  ".join(f"{k}: {v}" for k, v in m["close_reasons"].items())

    total_pnl_col = (
        f"[green]{m['total_pnl_pct']:+.2f}%[/]"
        if m["total_pnl_pct"] >= 0
        else f"[red]{m['total_pnl_pct']:+.2f}%[/]"
    )

    lines = (
        f"[bold]Asset:[/]  {symbol}   [bold]Timeframe:[/]  {timeframe}\n\n"
        f"Trades:          [bold]{m['total_trades']}[/]   "
        f"[green]W:{m['winners']}[/]  [red]L:{m['losers']}[/]\n"
        f"Win Rate:         [bold]{m['win_rate']*100:.1f}%[/]\n"
        f"Avg Win:          [green]{m['avg_win_pct']:+.2f}%[/]\n"
        f"Avg Loss:         [red]{m['avg_loss_pct']:+.2f}%[/]\n"
        f"Total PnL:        {total_pnl_col}\n"
        f"Profit Factor:    [bold]{pf_str}[/]\n"
        f"Max Drawdown:     [red]{m['max_drawdown']:.2f}%[/]\n"
        f"Sharpe (approx):  [bold]{sharpe_str}[/]\n"
        f"Exit Reasons:     {reasons_str}"
    )
    console.print(Panel(lines, title="📊  Backtest Results", border_style="cyan", expand=False))

    # Per-mode breakdown
    by_mode = m.get("by_mode", {})
    if by_mode:
        mode_tbl = Table(
            title="📈  Performance by Strategy Mode",
            box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
        )
        mode_tbl.add_column("Mode",        style="bold white",  width=14)
        mode_tbl.add_column("Trades",      style="white",       width=8,  justify="right")
        mode_tbl.add_column("W",           style="green",       width=5,  justify="right")
        mode_tbl.add_column("L",           style="red",         width=5,  justify="right")
        mode_tbl.add_column("Win %",       style="bold",        width=8,  justify="right")
        mode_tbl.add_column("Total PnL%",  style="bold",        width=12, justify="right")
        mode_tbl.add_column("Avg PnL%",    style="bold",        width=11, justify="right")
        mode_tbl.add_column("Top Exit",    style="dim",         width=16)
        for mode_label, md in by_mode.items():
            pnl_c  = "green" if md["total_pnl"] >= 0 else "red"
            avg_c  = "green" if md["avg_pnl"]   >= 0 else "red"
            top_exit = max(md["close_reasons"], key=md["close_reasons"].get) if md["close_reasons"] else "—"
            mode_tbl.add_row(
                mode_label,
                str(md["count"]),
                str(md["winners"]),
                str(md["count"] - md["winners"]),
                f"{md['win_rate']*100:.1f}%",
                f"[{pnl_c}]{md['total_pnl']:+.2f}%[/{pnl_c}]",
                f"[{avg_c}]{md['avg_pnl']:+.2f}%[/{avg_c}]",
                top_exit,
            )
        console.print(mode_tbl)

    # Phase 4: profit-leakage by mode
    try:
        from src.backtest.leakage_analyzer import analyze_leakage_by_mode, print_leakage_table
        leakage = analyze_leakage_by_mode(trades)
        if any(v.get("count", 0) > 0 for v in leakage.values()):
            console.print("\n[bold cyan]Profit Leakage by Strategy Mode[/bold cyan]")
            print_leakage_table(leakage)
    except Exception as _le:
        log.debug("Leakage report skipped: %s", _le)


def print_signal_quality_report(db_path: Any, signal_type: str = "BUY") -> None:
    """Print Phase 5 signal quality analytics tables from the signals DB."""
    try:
        from src.signals.signal_analytics import (
            accepted_vs_rejected_by_mode,
            top_rejection_reasons,
            ml_effect_summary,
            indicator_combination_summary,
            near_miss_signals,
        )
    except ImportError as exc:
        console.print(f"[yellow]Signal analytics unavailable: {exc}[/]")
        return

    console.print(
        Panel(
            f"Signal type: [bold]{signal_type}[/]",
            title="🔬  Phase 5 Signal Quality Report",
            border_style="magenta",
            expand=False,
        )
    )

    # ── Table 1: Accepted vs Rejected by Mode ─────────────────────────────────
    try:
        avr = accepted_vs_rejected_by_mode(db_path, signal_type)
        if avr:
            t1 = Table(
                title="✅  Accepted vs Rejected by Strategy Mode",
                box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
            )
            t1.add_column("Mode",        style="bold white", width=14)
            t1.add_column("Accepted",    style="green",      width=10, justify="right")
            t1.add_column("Rejected",    style="red",        width=10, justify="right")
            t1.add_column("Total",       style="white",      width=8,  justify="right")
            t1.add_column("Accept %",    style="bold",       width=10, justify="right")
            t1.add_column("Avg Score",   style="cyan",       width=10, justify="right")
            for mode_label, md in avr.items():
                rate   = md.get("accept_rate", 0.0)
                rate_c = "green" if rate >= 0.5 else "yellow" if rate >= 0.25 else "red"
                t1.add_row(
                    mode_label,
                    str(md.get("accepted", 0)),
                    str(md.get("rejected", 0)),
                    str(md.get("total",    0)),
                    f"[{rate_c}]{rate*100:.1f}%[/{rate_c}]",
                    f"{md.get('avg_score', 0.0):.1f}",
                )
            console.print(t1)
    except Exception as _e:
        log.debug("Table 1 (accepted_vs_rejected) skipped: %s", _e)

    # ── Table 2: Top Rejection Reasons ────────────────────────────────────────
    try:
        reasons = top_rejection_reasons(db_path, limit=10, signal_type=signal_type)
        if reasons:
            t2 = Table(
                title="🚫  Top Rejection Reasons",
                box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
            )
            t2.add_column("Reason",        style="red",         width=26)
            t2.add_column("Count",         style="white",       width=8,  justify="right")
            t2.add_column("Modes Affected",style="dim",         width=28)
            for row in reasons:
                modes_str = ", ".join(row.get("modes_affected", []))
                t2.add_row(
                    row.get("reason", ""),
                    str(row.get("count", 0)),
                    modes_str or "—",
                )
            console.print(t2)
    except Exception as _e:
        log.debug("Table 2 (rejection_reasons) skipped: %s", _e)

    # ── Table 3: ML/AI Effect Summary ─────────────────────────────────────────
    try:
        mle = ml_effect_summary(db_path, signal_type)
        if mle:
            t3 = Table(
                title="🤖  ML / AI Effect Summary",
                box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
            )
            t3.add_column("Gate",         style="bold white", width=6)
            t3.add_column("Vetoed",       style="red",        width=9,  justify="right")
            t3.add_column("Passed",       style="dim",        width=9,  justify="right")
            t3.add_column("Boosted",      style="green",      width=9,  justify="right")
            t3.add_column("Veto %",       style="red",        width=9,  justify="right")
            t3.add_column("Boost %",      style="green",      width=9,  justify="right")
            t3.add_row(
                "ML",
                str(mle.get("ml_vetoed",  0)),
                str(mle.get("ml_passed",  0)),
                str(mle.get("ml_boosted", 0)),
                f"{mle.get('ml_veto_rate',  0.0)*100:.1f}%",
                f"{mle.get('ml_boost_rate', 0.0)*100:.1f}%",
            )
            t3.add_row(
                "AI",
                str(mle.get("ai_vetoed",  0)),
                str(mle.get("ai_passed",  0)),
                str(mle.get("ai_boosted", 0)),
                f"{mle.get('ai_veto_rate',  0.0)*100:.1f}%",
                f"{mle.get('ai_boost_rate', 0.0)*100:.1f}%",
            )
            console.print(t3)
    except Exception as _e:
        log.debug("Table 3 (ml_effect) skipped: %s", _e)

    # ── Table 4: Indicator Combination Summary ────────────────────────────────
    try:
        combos = indicator_combination_summary(db_path, signal_type)
        if combos:
            t4 = Table(
                title="📐  Indicator Combination Performance",
                box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
            )
            t4.add_column("Alligator", style="cyan",   width=10)
            t4.add_column("Stochastic",style="cyan",   width=11)
            t4.add_column("Vortex",    style="cyan",   width=8)
            t4.add_column("Count",     style="white",  width=8,  justify="right")
            t4.add_column("Accept %",  style="bold",   width=10, justify="right")
            t4.add_column("Avg Score", style="cyan",   width=10, justify="right")
            for row in combos:
                rate   = row.get("accept_rate", 0.0)
                rate_c = "green" if rate >= 0.5 else "yellow" if rate >= 0.25 else "red"
                t4.add_row(
                    "Yes" if row.get("alligator_pt") else "No",
                    "Yes" if row.get("stochastic_pt") else "No",
                    "Yes" if row.get("vortex_pt") else "No",
                    str(row.get("count", 0)),
                    f"[{rate_c}]{rate*100:.1f}%[/{rate_c}]",
                    f"{row.get('avg_score', 0.0):.1f}",
                )
            console.print(t4)
    except Exception as _e:
        log.debug("Table 4 (indicator_combination) skipped: %s", _e)

    # ── Table 5: Near-Miss Signals ────────────────────────────────────────────
    try:
        near = near_miss_signals(db_path, signal_type=signal_type, limit=20)
        if near:
            t5 = Table(
                title="⚠️  Near-Miss Signals (score 60–70, valid but rejected)",
                box=box.HEAVY_HEAD, show_lines=True, style="white on grey7",
            )
            t5.add_column("Asset",       style="cyan",   width=10)
            t5.add_column("TF",          style="dim",    width=5)
            t5.add_column("Mode",        style="white",  width=13)
            t5.add_column("Score",       style="bold",   width=8,  justify="right")
            t5.add_column("Reject Reason", style="red",  width=26)
            t5.add_column("Entry Code",  style="dim",    width=18)
            t5.add_column("Time",        style="dim",    width=17)
            for row in near:
                t5.add_row(
                    row.get("asset", ""),
                    row.get("timeframe", ""),
                    row.get("mode", ""),
                    f"{row.get('score_total', 0.0):.1f}",
                    row.get("rejection_reason", "") or "—",
                    row.get("indicator_flags", "") or "—",
                    row.get("timestamp", "") or "—",
                )
            console.print(t5)
        else:
            console.print("[dim]No near-miss signals in range 60–70.[/]")
    except Exception as _e:
        log.debug("Table 5 (near_miss) skipped: %s", _e)


def export_csv(trades: list[TradeRecord], symbol: str, timeframe: str) -> Path:
    """Write trade ledger to CSV. Returns the file path."""
    closed   = [t for t in trades if t.status == "CLOSED"]
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    fname    = Path(DATA_DIR) / f"backtest_{symbol.replace('/','_')}_{timeframe}_{date_str}.csv"
    fname.parent.mkdir(parents=True, exist_ok=True)

    with open(fname, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trade_id", "signal_type", "asset", "timeframe", "strategy_mode",
            "entry_time", "exit_time", "duration",
            "entry_price", "exit_price", "close_reason",
            "pnl", "pnl_pct", "max_trail_reached",
            "alligator_pt", "stochastic_pt", "vortex_pt",
            "jaw_at_entry", "teeth_at_entry", "lips_at_entry",
            "ml_confidence", "ai_confidence",
        ])
        for t in closed:
            writer.writerow([
                t.trade_id, t.signal_type, t.asset, t.timeframe,
                getattr(t, "strategy_mode", "UNKNOWN"),
                _fmt_ts(t.entry_time), _fmt_ts(t.exit_time),
                _duration(t.entry_time, t.exit_time),
                f"{t.entry_price:.6f}",
                f"{t.exit_price:.6f}" if t.exit_price else "",
                t.close_reason,
                f"{t.pnl:.4f}", f"{t.pnl_pct:.4f}", f"{t.max_trail_reached:.6f}",
                int(t.alligator_point), int(t.stochastic_point), int(t.vortex_point),
                f"{t.jaw_at_entry:.6f}", f"{t.teeth_at_entry:.6f}", f"{t.lips_at_entry:.6f}",
                f"{t.ml_confidence:.3f}" if t.ml_confidence is not None else "",
                f"{t.ai_confidence:.3f}" if t.ai_confidence is not None else "",
            ])
    log.info("Backtest CSV written to %s", fname)
    return fname
