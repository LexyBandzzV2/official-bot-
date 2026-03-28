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
        r = t.close_reason or "UNKNOWN"
        by_reason[r] = by_reason.get(r, 0) + 1

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
        reason   = (t.close_reason or "").replace("_", " ").title()
        tbl.add_row(
            str(i),
            type_col,
            t.asset,
            t.timeframe,
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


def export_csv(trades: list[TradeRecord], symbol: str, timeframe: str) -> Path:
    """Write trade ledger to CSV. Returns the file path."""
    closed   = [t for t in trades if t.status == "CLOSED"]
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    fname    = Path(DATA_DIR) / f"backtest_{symbol.replace('/','_')}_{timeframe}_{date_str}.csv"
    fname.parent.mkdir(parents=True, exist_ok=True)

    with open(fname, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trade_id", "signal_type", "asset", "timeframe",
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
