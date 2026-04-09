"""Rich terminal display — every signal, order, close, and status event
prints an organised, colour-coded table using the `rich` library.

All times are printed in the user's configured timezone (America/Toronto
by default) so every line is pinpointable for research.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
import pytz

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text
from rich.columns import Columns

from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord

console = Console()

try:
    from src.config import TIMEZONE, ML_CONFIDENCE_THRESHOLD, AI_CONFIDENCE_THRESHOLD
except ImportError:
    TIMEZONE = "America/Toronto"
    ML_CONFIDENCE_THRESHOLD = 0.65
    AI_CONFIDENCE_THRESHOLD = 0.60

_tz = pytz.timezone(TIMEZONE)


def _now_str() -> str:
    return datetime.now(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")


def _fmt_price(p: float) -> str:
    return f"${p:,.5f}"


def _fmt_pct(p: float, signed: bool = True) -> str:
    sign = "+" if signed and p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _tick(val: bool) -> str:
    return "[green]✓[/green]" if val else "[red]✗[/red]"


# ── BUY signal ────────────────────────────────────────────────────────────────

def print_buy_signal(sig: BuySignalResult) -> None:
    ts = sig.timestamp.astimezone(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")

    t = Table(box=box.HEAVY_HEAD, show_header=False, padding=(0, 1),
              border_style="green")
    t.add_column("Field",  style="bold white",  no_wrap=True)
    t.add_column("Value",  style="bright_white", no_wrap=False)

    t.add_row("Symbol",           f"[bold cyan]{sig.asset}[/bold cyan]")
    t.add_row("Asset Class",      _asset_class_label(sig.asset))
    t.add_row("Timeframe",        sig.timeframe)
    t.add_row("Strategy Mode",    f"[bold]{getattr(sig, 'strategy_mode', 'UNKNOWN')}[/bold]")
    t.add_row("Signal Date/Time", f"[bold]{ts}[/bold]")
    t.add_row("Points",           f"[bold green]{sig.points}/3  ✓[/bold green]" if sig.points == 3 else f"{sig.points}/3")
    t.add_row("─" * 20,           "─" * 35)
    t.add_row("Alligator",        f"{_tick(sig.alligator_point)}  Lips above teeth + jaw")
    t.add_row("Stochastic",       f"{_tick(sig.stochastic_point)}  K or D entered above 80")
    t.add_row("Vortex",           f"{_tick(sig.vortex_point)}  VI+ crossed above VI-")
    t.add_row("─" * 20,           "─" * 35)
    t.add_row("Entry Price",      f"[bold yellow]{_fmt_price(sig.entry_price)}[/bold yellow]")
    t.add_row("Stop Loss (2%)",   f"[red]{_fmt_price(sig.stop_loss)}  (hard floor)[/red]")
    t.add_row("Est. Move",        f"[green]{_fmt_pct(sig.profit_estimate_pct)}[/green]")
    t.add_row("Exit Trigger",     "Lips touches / crosses down to Teeth (exit long)")

    if sig.ai_confidence is not None:
        conf_col = "green" if sig.ai_confidence >= AI_CONFIDENCE_THRESHOLD else "yellow"
        t.add_row("AI Confidence", f"[{conf_col}]{sig.ai_confidence*100:.0f}%  (LM Studio)[/{conf_col}]")
    if sig.ml_confidence is not None:
        ml_col = "green" if sig.ml_confidence >= ML_CONFIDENCE_THRESHOLD else "yellow"
        label  = "PASS" if sig.ml_confidence >= ML_CONFIDENCE_THRESHOLD else "WARN"
        t.add_row("ML Filter", f"[{ml_col}]{label} — predicted win prob: {sig.ml_confidence*100:.0f}%[/{ml_col}]")

    console.print(Panel(t, title="[bold green]🟢  BUY SIGNAL[/bold green]",
                        border_style="green", expand=False))


# ── SELL signal ───────────────────────────────────────────────────────────────

def print_sell_signal(sig: SellSignalResult) -> None:
    ts = sig.timestamp.astimezone(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")

    t = Table(box=box.HEAVY_HEAD, show_header=False, padding=(0, 1),
              border_style="red")
    t.add_column("Field",  style="bold white",  no_wrap=True)
    t.add_column("Value",  style="bright_white", no_wrap=False)

    t.add_row("Symbol",           f"[bold cyan]{sig.asset}[/bold cyan]")
    t.add_row("Asset Class",      _asset_class_label(sig.asset))
    t.add_row("Timeframe",        sig.timeframe)
    t.add_row("Strategy Mode",    f"[bold]{getattr(sig, 'strategy_mode', 'UNKNOWN')}[/bold]")
    t.add_row("Signal Date/Time", f"[bold]{ts}[/bold]")
    t.add_row("Points",           f"[bold red]{sig.points}/3  ✓[/bold red]" if sig.points == 3 else f"{sig.points}/3")
    t.add_row("─" * 20,           "─" * 35)
    t.add_row("Alligator",        f"{_tick(sig.alligator_point)}  Lips below teeth + jaw")
    t.add_row("Stochastic",       f"{_tick(sig.stochastic_point)}  K or D entered below 20")
    t.add_row("Vortex",           f"{_tick(sig.vortex_point)}  VI- crossed above VI+")
    t.add_row("─" * 20,           "─" * 35)
    t.add_row("Entry Price",      f"[bold yellow]{_fmt_price(sig.entry_price)}[/bold yellow]")
    t.add_row("Stop Loss (2%)",   f"[red]{_fmt_price(sig.stop_loss)}  (hard ceiling)[/red]")
    t.add_row("Est. Move",        f"[green]{_fmt_pct(sig.profit_estimate_pct)}[/green]")
    t.add_row("Exit Trigger",     "Lips touches / crosses up to Teeth (exit short)")

    if sig.ai_confidence is not None:
        conf_col = "green" if sig.ai_confidence >= AI_CONFIDENCE_THRESHOLD else "yellow"
        t.add_row("AI Confidence", f"[{conf_col}]{sig.ai_confidence*100:.0f}%  (LM Studio)[/{conf_col}]")
    if sig.ml_confidence is not None:
        ml_col = "green" if sig.ml_confidence >= ML_CONFIDENCE_THRESHOLD else "yellow"
        label  = "PASS" if sig.ml_confidence >= ML_CONFIDENCE_THRESHOLD else "WARN"
        t.add_row("ML Filter", f"[{ml_col}]{label} — predicted win prob: {sig.ml_confidence*100:.0f}%[/{ml_col}]")

    console.print(Panel(t, title="[bold red]🔴  SELL SIGNAL[/bold red]",
                        border_style="red", expand=False))


# ── Order placed ─────────────────────────────────────────────────────────────

def print_order_placed(rec: TradeRecord, fill_price: float, slippage_pips: float = 0.0) -> None:
    ts = rec.entry_time.astimezone(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")
    color = "green" if rec.signal_type == "BUY" else "red"

    t = Table(box=box.HEAVY_HEAD, show_header=False, padding=(0, 1),
              border_style=color)
    t.add_column("Field", style="bold white",  no_wrap=True)
    t.add_column("Value", style="bright_white", no_wrap=False)

    t.add_row("Symbol",          f"[bold cyan]{rec.asset}[/bold cyan]")
    t.add_row("Direction",       f"[bold {color}]{rec.signal_type}[/bold {color}]")
    t.add_row("Execution Time",  f"[bold]{ts}[/bold]")
    t.add_row("Fill Price",      _fmt_price(fill_price))
    t.add_row("Slippage",        f"{slippage_pips:+.1f} pips")
    t.add_row("Position Size",   f"{rec.position_size:.4f} units")
    t.add_row("Account Risk",    f"[yellow]{rec.account_risk_pct:.2f}%[/yellow]")
    t.add_row("Hard Stop (2%)",  f"[red]{_fmt_price(rec.stop_loss_hard)}[/red]")
    t.add_row("Trailing Stop",   f"[cyan]{_fmt_price(rec.trailing_stop)}  (Teeth line, ratchets)[/cyan]")

    console.print(Panel(t, title=f"[bold {color}]⚡ ORDER PLACED[/bold {color}]",
                        border_style=color, expand=False))


# ── Trade closed ─────────────────────────────────────────────────────────────

def print_trade_closed(rec: TradeRecord) -> None:
    entry_ts = rec.entry_time.astimezone(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")
    exit_ts  = rec.exit_time.astimezone(_tz).strftime("%Y-%m-%d  %I:%M:%S %p %Z")  # type: ignore
    duration = str(rec.exit_time - rec.entry_time).split(".")[0]  # type: ignore

    pnl_col  = "green" if rec.pnl >= 0 else "red"
    sign     = "+" if rec.pnl >= 0 else ""
    dir_col  = "green" if rec.signal_type == "BUY" else "red"

    t = Table(box=box.HEAVY_HEAD, show_header=False, padding=(0, 1),
              border_style=pnl_col)
    t.add_column("Field", style="bold white",  no_wrap=True)
    t.add_column("Value", style="bright_white", no_wrap=False)

    t.add_row("Symbol",            f"[bold cyan]{rec.asset}[/bold cyan]")
    t.add_row("Direction",         f"[bold {dir_col}]{rec.signal_type}[/bold {dir_col}]")
    t.add_row("Strategy Mode",     f"[bold]{getattr(rec, 'strategy_mode', 'UNKNOWN')}[/bold]")
    t.add_row("Entry Date/Time",   f"[bold]{entry_ts}[/bold]")
    t.add_row("Exit  Date/Time",   f"[bold]{exit_ts}[/bold]")
    t.add_row("Duration",          duration)
    t.add_row("Entry Price",       _fmt_price(rec.entry_price))
    t.add_row("Exit  Price",       _fmt_price(rec.exit_price))  # type: ignore
    cr = rec.close_reason or "—"
    # Canonical label mapping for human-readable display.
    # TRAILING_TP alias handles any legacy DB rows not yet migrated.
    _REASON_LABELS = {
        "PEAK_GIVEBACK_EXIT": "Peak Giveback Exit",
        "TRAILING_TP":        "Peak Giveback Exit",  # legacy alias
        "HARD_STOP":          "Hard Stop",
        "TRAIL_STOP":         "Trailing Stop",
        "ALLIGATOR_TP":       "Alligator TP",
        "MANUAL":             "Manual",
    }
    cr = _REASON_LABELS.get(cr, str(cr).replace("_", " ").title())
    t.add_row("Close Reason",      f"[bold]{cr}[/bold]")
    t.add_row("Max Trail Reached", _fmt_price(rec.max_trail_reached))
    t.add_row("PnL ($)",           f"[bold {pnl_col}]{sign}${rec.pnl:.2f}[/bold {pnl_col}]")
    t.add_row("PnL (%)",           f"[bold {pnl_col}]{sign}{rec.pnl_pct:.2f}%[/bold {pnl_col}]")

    console.print(Panel(t, title="[bold]🔔 TRADE CLOSED[/bold]",
                        border_style=pnl_col, expand=False))


# ── Active signals table ─────────────────────────────────────────────────────

def print_active_signals(records: List[TradeRecord]) -> None:
    if not records:
        console.print("[dim]No open positions.[/dim]")
        return

    t = Table(
        title=f"OPEN POSITIONS — {_now_str()}",
        box=box.ROUNDED, border_style="cyan",
        show_lines=True,
    )
    t.add_column("Symbol",    style="bold cyan",   no_wrap=True)
    t.add_column("Class",     style="white",       no_wrap=True)
    t.add_column("Dir",       style="bold",        no_wrap=True)
    t.add_column("Entry",     style="yellow",      no_wrap=True)
    t.add_column("Entry Time",style="dim",         no_wrap=True)
    t.add_column("Trail SL",  style="red",         no_wrap=True)
    t.add_column("AI Conf",   style="green",       no_wrap=True)

    for rec in records:
        dir_col = "green" if rec.signal_type == "BUY" else "red"
        t.add_row(
            rec.asset,
            _asset_class_label(rec.asset, plain=True),
            f"[{dir_col}]{rec.signal_type}[/{dir_col}]",
            _fmt_price(rec.entry_price),
            rec.entry_time.astimezone(_tz).strftime("%Y-%m-%d %I:%M %p"),
            _fmt_price(rec.trailing_stop),
            f"{rec.ai_confidence:.0f}%" if rec.ai_confidence else "—",
        )
    console.print(t)


# ── Signal history table ─────────────────────────────────────────────────────

def print_signal_history(rows: list, title: str = "SIGNAL HISTORY") -> None:
    if not rows:
        console.print("[dim]No signals found.[/dim]")
        return

    t = Table(title=title, box=box.ROUNDED, border_style="white", show_lines=True)
    t.add_column("#",          style="dim",         no_wrap=True)
    t.add_column("Date/Time",  style="dim",         no_wrap=True)
    t.add_column("Symbol",     style="bold cyan",   no_wrap=True)
    t.add_column("Mode",       style="white",       no_wrap=True)
    t.add_column("Dir",        style="bold",        no_wrap=True)
    t.add_column("Pts",        style="white",       no_wrap=True)
    t.add_column("Entry",      style="yellow",      no_wrap=True)
    t.add_column("Exit",       style="yellow",      no_wrap=True)
    t.add_column("Close",      style="white",       no_wrap=True)
    t.add_column("PnL%",       style="bold",        no_wrap=True)

    for i, r in enumerate(rows, 1):
        pnl_pct = r.get("pnl_pct", 0.0) or 0.0
        pnl_col = "green" if pnl_pct >= 0 else "red"
        sign    = "+" if pnl_pct >= 0 else ""
        dir_    = r.get("signal_type", "?")
        dc      = "green" if dir_ == "BUY" else "red"
        t.add_row(
            str(i),
            str(r.get("entry_time", ""))[:16],
            r.get("asset", "?"),
            r.get("strategy_mode", "—"),
            f"[{dc}]{dir_}[/{dc}]",
            f"{r.get('points',0)}/3",
            _fmt_price(r.get("entry_price", 0)),
            _fmt_price(r.get("exit_price", 0)) if r.get("exit_price") else "—",
            r.get("close_reason", "—"),
            f"[{pnl_col}]{sign}{pnl_pct:.2f}%[/{pnl_col}]",
        )
    console.print(t)


# ── Trailing stop update (inline, one line) ───────────────────────────────────

def print_trail_update(
    asset: str, old_stop: float, new_stop: float, direction: str
) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    locked_dir = "▲" if direction == "buy" else "▼"
    console.print(
        f"[dim]{ts}[/dim]  [cyan]{asset}[/cyan] "
        f"[bold {'green' if direction=='buy' else 'red'}]{direction.upper()}[/bold {'green' if direction=='buy' else 'red'}]  "
        f"Trail updated: [yellow]{_fmt_price(old_stop)}[/yellow] → "
        f"[bold green]{_fmt_price(new_stop)}[/bold green] {locked_dir}"
    )


# ── Daily kill switch alert ───────────────────────────────────────────────────

def print_kill_switch(loss_pct: float) -> None:
    console.print(Panel(
        f"[bold red]Daily loss reached {loss_pct:.2f}% — scanner suspended until next session.\n"
        "All pending signals cleared.  Bot in standby mode.[/bold red]",
        title="[bold red]⛔  DAILY KILL SWITCH TRIGGERED[/bold red]",
        border_style="red",
    ))


# ── Status summary ────────────────────────────────────────────────────────────

def print_status(
    account_balance: float,
    daily_loss_pct:  float,
    open_count:      int,
    hourly_remaining:int,
    scanner_running: bool,
) -> None:
    t = Table(box=box.ROUNDED, show_header=False, border_style="cyan", padding=(0, 1))
    t.add_column("k", style="bold white",  no_wrap=True)
    t.add_column("v", style="bright_white", no_wrap=False)

    t.add_row("Scanner",          "[green]RUNNING[/green]" if scanner_running else "[red]STOPPED[/red]")
    t.add_row("Time",             _now_str())
    t.add_row("Account Balance",  f"[bold yellow]${account_balance:,.2f}[/bold yellow]")
    t.add_row("Daily Loss",       f"[{'red' if daily_loss_pct > 5 else 'yellow'}]{daily_loss_pct:.2f}%[/]  (limit 10%)")
    t.add_row("Open Positions",   str(open_count))
    t.add_row("Trades Left / hr", str(hourly_remaining))

    console.print(Panel(t, title="[bold cyan]AlgoBot Status[/bold cyan]",
                        border_style="cyan", expand=False))


# ── P&L summary ──────────────────────────────────────────────────────────────

def print_pnl_summary(rows: list) -> None:
    if not rows:
        console.print("[dim]No closed trades.[/dim]")
        return

    total_pnl   = sum(r.get("pnl", 0) for r in rows)
    winning     = [r for r in rows if r.get("pnl", 0) > 0]
    losing      = [r for r in rows if r.get("pnl", 0) < 0]
    win_rate    = len(winning) / len(rows) * 100 if rows else 0

    t = Table(title=f"P&L SUMMARY — {len(rows)} trades", box=box.ROUNDED,
              border_style="cyan", show_lines=False)
    t.add_column("Metric", style="bold white", no_wrap=True)
    t.add_column("Value",  style="bright_white", no_wrap=True)

    pnl_col = "green" if total_pnl >= 0 else "red"
    t.add_row("Total PnL",    f"[bold {pnl_col}]${total_pnl:+,.2f}[/bold {pnl_col}]")
    t.add_row("Win Rate",     f"{win_rate:.1f}%  ({len(winning)}W / {len(losing)}L)")
    t.add_row("Avg Win",      f"${sum(r.get('pnl',0) for r in winning)/len(winning):.2f}" if winning else "—")
    t.add_row("Avg Loss",     f"${sum(r.get('pnl',0) for r in losing)/len(losing):.2f}"   if losing  else "—")
    t.add_row("Total Trades", str(len(rows)))
    console.print(t)


# ── Internal helper ───────────────────────────────────────────────────────────

def _asset_class_label(symbol: str, plain: bool = False) -> str:
    try:
        from src.data.symbol_mapper import get_asset_class
        cls = get_asset_class(symbol)
    except Exception:
        cls = "unknown"
    colours = {"forex": "blue", "crypto": "magenta", "stock": "white",
               "commodity": "yellow", "index": "cyan"}
    col = colours.get(cls, "white")
    if plain:
        return cls.capitalize()
    return f"[{col}]{cls.capitalize()}[/{col}]"
