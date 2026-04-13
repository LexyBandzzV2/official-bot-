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
from rich.rule import Rule

from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord

console = Console()

try:
    from src.config import TIMEZONE, AI_CONFIDENCE_THRESHOLD
except ImportError:
    TIMEZONE = "America/Toronto"
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
    ts = sig.timestamp.astimezone(_tz).strftime("%I:%M:%S %p")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), border_style="green")
    t.add_column("k", style="bold white",   no_wrap=True, min_width=12)
    t.add_column("v", style="bright_white", no_wrap=False)

    t.add_row("Symbol",     f"[bold cyan]{sig.asset}[/bold cyan]  [dim]{sig.timeframe}[/dim]  {_asset_class_label(sig.asset)}")
    t.add_row("Direction",  "[bold green]LONG  ▲[/bold green]")
    t.add_row("Entry",      f"[bold yellow]{_fmt_price(sig.entry_price)}[/bold yellow]")
    t.add_row("Stop Loss",  f"[red]{_fmt_price(sig.stop_loss)}[/red]  [dim](-2% hard floor)[/dim]")
    t.add_row("Est. Move",  f"[bold green]{_fmt_pct(sig.profit_estimate_pct)}[/bold green]")
    t.add_row("Strategy",   f"[bold]{getattr(sig, 'strategy_mode', 'SCALP')}[/bold]")
    t.add_row("Indicators", (
        f"{_tick(sig.alligator_point)} Alligator  "
        f"{_tick(sig.stochastic_point)} Stoch  "
        f"{_tick(sig.vortex_point)} Vortex  "
        f"[bold green]{sig.points}/3[/bold green]"
    ))
    _mtf = getattr(sig, "mtf_alignment", None)
    if _mtf:
        _mtf_colors = {"ALIGNED": "bold green", "NEUTRAL": "dim white", "COUNTER": "bold red", "UNAVAILABLE": "dim"}
        t.add_row("MTF", f"[{_mtf_colors.get(_mtf, 'white')}]{_mtf}[/{_mtf_colors.get(_mtf, 'white')}]")
    if sig.ai_confidence is not None:
        conf_col = "green" if sig.ai_confidence >= 0.50 else "yellow"
        t.add_row("AI Conf",  f"[{conf_col}]{sig.ai_confidence*100:.0f}%[/{conf_col}]")
    score = getattr(sig, "score_total", None)
    if score is not None:
        t.add_row("Score",  f"[bold white]{score:.0f}[/bold white]")

    console.print(Panel(
        t,
        title=f"[bold green]🟢  BUY SIGNAL  [dim]{ts}[/dim][/bold green]",
        border_style="green",
        expand=False,
    ))


# ── SELL signal ───────────────────────────────────────────────────────────────

def print_sell_signal(sig: SellSignalResult) -> None:
    ts = sig.timestamp.astimezone(_tz).strftime("%I:%M:%S %p")

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), border_style="red")
    t.add_column("k", style="bold white",   no_wrap=True, min_width=12)
    t.add_column("v", style="bright_white", no_wrap=False)

    t.add_row("Symbol",     f"[bold cyan]{sig.asset}[/bold cyan]  [dim]{sig.timeframe}[/dim]  {_asset_class_label(sig.asset)}")
    t.add_row("Direction",  "[bold red]SHORT  ▼[/bold red]")
    t.add_row("Entry",      f"[bold yellow]{_fmt_price(sig.entry_price)}[/bold yellow]")
    t.add_row("Stop Loss",  f"[red]{_fmt_price(sig.stop_loss)}[/red]  [dim](+2% hard ceiling)[/dim]")
    t.add_row("Est. Move",  f"[bold green]{_fmt_pct(sig.profit_estimate_pct)}[/bold green]")
    t.add_row("Strategy",   f"[bold]{getattr(sig, 'strategy_mode', 'SCALP')}[/bold]")
    t.add_row("Indicators", (
        f"{_tick(sig.alligator_point)} Alligator  "
        f"{_tick(sig.stochastic_point)} Stoch  "
        f"{_tick(sig.vortex_point)} Vortex  "
        f"[bold red]{sig.points}/3[/bold red]"
    ))
    _mtf = getattr(sig, "mtf_alignment", None)
    if _mtf:
        _mtf_colors = {"ALIGNED": "bold green", "NEUTRAL": "dim white", "COUNTER": "bold red", "UNAVAILABLE": "dim"}
        t.add_row("MTF", f"[{_mtf_colors.get(_mtf, 'white')}]{_mtf}[/{_mtf_colors.get(_mtf, 'white')}]")
    if sig.ai_confidence is not None:
        conf_col = "green" if sig.ai_confidence >= 0.50 else "yellow"
        t.add_row("AI Conf",  f"[{conf_col}]{sig.ai_confidence*100:.0f}%[/{conf_col}]")
    score = getattr(sig, "score_total", None)
    if score is not None:
        t.add_row("Score",  f"[bold white]{score:.0f}[/bold white]")

    console.print(Panel(
        t,
        title=f"[bold red]🔴  SELL SIGNAL  [dim]{ts}[/dim][/bold red]",
        border_style="red",
        expand=False,
    ))


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
    account_balance:  float = 0.0,
    daily_loss_pct:   float = 0.0,
    open_count:       int   = 0,
    hourly_remaining: int   = 0,
    scanner_running:  bool  = False,
    # System health fields (used by the `status` CLI command)
    lm_studio_ok:   bool  = False,
    openrouter_ok:  bool  = False,
    finnhub_ok:     bool  = False,
    supabase_ok:    bool  = False,
    ml_ready:       bool  = False,
    ml_samples:     int   = 0,
    open_trades:    int   = 0,
    closed_trades:  int   = 0,
    timezone:       str   = "",
) -> None:
    t = Table(box=box.ROUNDED, show_header=False, border_style="cyan", padding=(0, 1))
    t.add_column("k", style="bold white",  no_wrap=True)
    t.add_column("v", style="bright_white", no_wrap=False)

    t.add_row("Scanner",          "[green]RUNNING[/green]" if scanner_running else "[dim]stopped[/dim]")
    t.add_row("Time",             _now_str())
    if account_balance:
        t.add_row("Account Balance",  f"[bold yellow]${account_balance:,.2f}[/bold yellow]")
    if daily_loss_pct:
        t.add_row("Daily Loss",   f"[{'red' if daily_loss_pct > 5 else 'yellow'}]{daily_loss_pct:.2f}%[/]")
    t.add_row("Open Positions",   str(open_trades or open_count))
    t.add_row("Closed Trades",    str(closed_trades))
    if hourly_remaining:
        t.add_row("Trades Left / hr", str(hourly_remaining))

    # System health section
    def _ok(v: bool) -> str:
        return "[green]OK[/green]" if v else "[red]NOT CONNECTED[/red]"

    t.add_row("LM Studio",    _ok(lm_studio_ok))
    t.add_row("OpenRouter",   _ok(openrouter_ok))
    t.add_row("Finnhub",      _ok(finnhub_ok))
    t.add_row("Supabase",     _ok(supabase_ok))
    t.add_row("ML Model",     f"[green]READY ({ml_samples} samples)[/green]" if ml_ready else "[yellow]NOT TRAINED[/yellow]")
    if timezone:
        t.add_row("Timezone", timezone)

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


# ── Rejection event (compact card) ───────────────────────────────────────────

def print_rejected(
    signal_type: str,
    asset: str,
    timeframe: str,
    reason: str,
    entry: Optional[float] = None,
    ai_conf: Optional[float] = None,
) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    dir_col = "green" if signal_type == "BUY" else "red"

    # Map raw reason codes to plain English
    _REASON_MAP = {
        "broker_router_rejected_or_not_placed": "Broker order rejected / not placed",
        "CONFLICT_SUPPRESSED": "Conflicting BUY+SELL — both suppressed",
        "broker_unavailable": "No broker connected",
        "position_size_zero": "Position size calculated as zero",
        "regime_size_factor_zeroed": "Regime filter zeroed position size",
    }
    if reason.startswith("DUPLICATE_POSITION"):
        display_reason = f"Already in a position ({asset})"
    elif reason.startswith("AI rejected"):
        display_reason = reason  # already readable
    else:
        display_reason = _REASON_MAP.get(reason, reason)

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), border_style="yellow")
    t.add_column("k", style="bold white", no_wrap=True, min_width=10)
    t.add_column("v", style="bright_white", no_wrap=False)

    t.add_row("Symbol",    f"[bold cyan]{asset}[/bold cyan]  [dim]{timeframe}[/dim]")
    t.add_row("Direction", f"[bold {dir_col}]{signal_type}[/bold {dir_col}]")
    if entry is not None:
        t.add_row("Entry",  f"[yellow]{_fmt_price(entry)}[/yellow]")
    if ai_conf is not None:
        t.add_row("AI Conf", f"{ai_conf * 100:.0f}%")
    t.add_row("Reason",    f"[yellow]{display_reason}[/yellow]")

    console.print(Panel(
        t,
        title=f"[yellow]⚠  REJECTED  [dim]{ts}[/dim][/yellow]",
        border_style="yellow",
        expand=False,
    ))


# ── Broker error panel ────────────────────────────────────────────────────────

def print_broker_error(
    symbol: str,
    direction: str,
    http_status: int,
    error_summary: str,
) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), border_style="red")
    t.add_column("k", style="bold white", no_wrap=True, min_width=12)
    t.add_column("v", style="bright_white", no_wrap=False)

    dir_col = "green" if direction.upper() == "BUY" else "red"
    t.add_row("Symbol",    f"[bold cyan]{symbol}[/bold cyan]")
    t.add_row("Direction", f"[bold {dir_col}]{direction.upper()}[/bold {dir_col}]")
    t.add_row("HTTP",      f"[red]{http_status}[/red]")
    t.add_row("Error",     f"[red]{error_summary}[/red]")

    console.print(Panel(
        t,
        title=f"[bold red]🔴  BROKER ERROR  [dim]{ts}[/dim][/bold red]",
        border_style="red",
        expand=False,
    ))


# ── Regime change (compact inline) ────────────────────────────────────────────

def print_regime_change(
    symbol: str,
    timeframe: str,
    regime: str,
    conf: float,
    evidence: str,
) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    conf_col = "green" if conf >= 0.70 else "yellow" if conf >= 0.50 else "dim"
    console.print(
        f"[dim]{ts}[/dim]  [bold cyan]📊 Regime[/bold cyan]  "
        f"[bold]{symbol}[/bold] [dim]{timeframe}[/dim]  "
        f"[bold white]{regime}[/bold white]  "
        f"[{conf_col}]conf={conf:.2f}[/{conf_col}]  "
        f"[dim]{evidence}[/dim]"
    )


# ── Regime score bias (compact inline) ────────────────────────────────────────

def print_regime_bias(
    symbol: str,
    timeframe: str,
    signal_type: str,
    old_score: float,
    new_score: float,
    bias: float,
    reason: str,
) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    bias_col = "green" if bias >= 0 else "red"
    dir_col = "green" if signal_type == "BUY" else "red"
    console.print(
        f"[dim]{ts}[/dim]  [bold magenta]⚖  Regime Bias[/bold magenta]  "
        f"[bold]{symbol}[/bold] [dim]{timeframe}[/dim]  "
        f"[{dir_col}]{signal_type}[/{dir_col}]  "
        f"score [dim]{old_score:.0f}[/dim] → [bold white]{new_score:.0f}[/bold white]  "
        f"[{bias_col}]{bias:+.1f}[/{bias_col}]  [dim]({reason})[/dim]"
    )


# ── AI unavailable notice (single dim line) ───────────────────────────────────

def print_ai_unavailable(signal_type: str, asset: str, score: float = 0.4) -> None:
    ts = datetime.now(_tz).strftime("%I:%M:%S %p")
    dir_col = "green" if signal_type == "BUY" else "red"
    console.print(
        f"[dim]{ts}  🤖 No AI — using neutral score {score:.0f}%  "
        f"[{dir_col}]{signal_type}[/{dir_col}] {asset}[/dim]"
    )


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
