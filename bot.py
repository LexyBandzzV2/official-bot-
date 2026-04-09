#!/usr/bin/env python3
"""AlgoBot — Multi-Market Signal Detection & Algorithmic Trading Bot.

Usage examples:
    python bot.py scan --ticker EURUSD --timeframe 1h
    python bot.py scan --all --timeframe 4h
    python bot.py scan --category crypto --timeframe 1h
    python bot.py scan --ticker BTC/USDT --historical --days 30

    python bot.py signals --history
    python bot.py signals --open
    python bot.py signals --type BUY --limit 20

    python bot.py trades --log
    python bot.py trades --today
    python bot.py trades --pnl

    python bot.py backtest --ticker EURUSD --timeframe 1h --days 180
    python bot.py backtest --all --timeframe 4h --days 90

    python bot.py assets --list
    python bot.py assets --add BTC/USDT --category crypto
    python bot.py assets --remove GBPUSD

    python bot.py status

    python bot.py start --timeframe 1h --dry-run
    python bot.py start --timeframe 1h --live
    python bot.py stop

    python bot.py ai debrief
    python bot.py ml train
    python bot.py ml status
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel   import Panel
from rich.table   import Table
from rich         import box

console = Console()

# Ensure src/ is importable when running bot.py from project root
sys.path.insert(0, str(Path(__file__).parent))

# ── Lazy imports (so `--help` is instant even without dependencies) ───────────

def _imports():
    """Import heavy modules only when a command actually runs."""
    from src.notifications.logger import setup_logging
    setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
#  Root group
# ══════════════════════════════════════════════════════════════════════════════

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """AlgoBot — Heikin Ashi signal detection with Alligator · Stochastic · Vortex."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
#  scan
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("scan")
@click.option("--ticker",     "-t",  default=None,  help="Symbol to scan  (e.g. EURUSD, BTC/USDT)")
@click.option("--all",        "scan_all", is_flag=True, help="Scan every asset in the catalogue")
@click.option("--category",   "-c",  default=None,
              type=click.Choice(["forex","crypto","stocks","commodities","indices"], case_sensitive=False),
              help="Scan all symbols in a category")
@click.option("--timeframe",  "-tf", default="1h",  show_default=True, help="Candle timeframe")
@click.option("--historical", is_flag=True,          help="Scan historical candles (not live)")
@click.option("--days",       "-d",  default=30,    show_default=True, help="Days of history to scan")
@click.option("--top",              default=10,     show_default=True, help="Max candidates to deep-scan")
@click.option("--ohlc",       is_flag=True,          help="Use standard OHLC candles instead of Heikin Ashi")
def cmd_scan(ticker, scan_all, category, timeframe, historical, days, top, ohlc):
    """Scan markets for BUY/SELL signals."""
    _imports()
    import pandas as pd
    from src.data.heikin_ashi      import convert_to_heikin_ashi
    from src.data.market_data      import get_historical_ohlcv, get_latest_candles
    from src.data.symbol_mapper    import get_all_symbols, get_symbols_by_class, best_source
    from src.signals.signal_engine import SignalEngine
    from src.display.tables        import print_buy_signal, print_sell_signal
    from src.data.db               import init_db, save_signal, get_recent_signals
    from src.scanner.candidate_ranker import score_symbol, rank_candidates

    init_db()

    # Build symbol list
    if ticker:
        symbols = [ticker.upper()]
    elif category:
        symbols = get_symbols_by_class(category)
    elif scan_all:
        from src.data.kraken_assets import get_kraken_pairs
        kraken_pairs = get_kraken_pairs()
        symbols = list(set(get_all_symbols()) | set(kraken_pairs))
    else:
        console.print("[red]Specify --ticker, --all, or --category[/]")
        raise SystemExit(1)

    # ── ASCII Art Banner ──────────────────────────────────────────────────────
    BANNER = (
        "[bold cyan]"
        " ██████  ██     ██ ██          ███████ ████████  █████  ██      ██   ██ \n"
        "██    ██ ██     ██ ██          ██         ██    ██   ██ ██      ██  ██  \n"
        "██    ██ ██  █  ██ ██          ███████    ██    ███████ ██      █████   \n"
        "██    ██ ██ ███ ██ ██               ██    ██    ██   ██ ██      ██  ██  \n"
        " ██████   ███ ███  ███████     ███████    ██    ██   ██ ███████ ██   ██ "
        "[/bold cyan]"
    )
    console.print(BANNER)
    console.print()

    mode_lbl  = "[green]HISTORICAL[/green]" if historical else "[bold green]LIVE[/bold green]"
    ohlc_lbl  = "  [dim](OHLC mode)[/dim]" if ohlc else ""
    console.print(
        f"[bold white]  AlgoBot Scan[/bold white]  ·  "
        f"Symbols: [bold yellow]{len(symbols)}[/bold yellow]  ·  "
        f"TF: [bold yellow]{timeframe}[/bold yellow]  ·  "
        f"Mode: {mode_lbl}{ohlc_lbl}"
    )
    console.rule(style="cyan")

    import pytz
    from src.config import TIMEZONE
    tz = pytz.timezone(TIMEZONE)

    for sym in symbols:
        try:
            if historical:
                start = datetime.now(timezone.utc) - timedelta(days=days)
                raw   = get_historical_ohlcv(sym, timeframe, start=start, source=best_source(sym))
            else:
                raw   = get_latest_candles(sym, timeframe, count=200, source=best_source(sym))

            if raw.empty:
                console.print(f"[dim]{sym:<12}[/dim]  [dim]no data[/dim]")
                continue

            if ohlc:
                target_df = raw
            else:
                target_df = convert_to_heikin_ashi(raw)
                # Overwrite base columns with HA values so indicators calculate using smoothed data
                target_df["open"]  = target_df["ha_open"]
                target_df["high"]  = target_df["ha_high"]
                target_df["low"]   = target_df["ha_low"]
                target_df["close"] = target_df["ha_close"]

            engine    = SignalEngine(sym, timeframe)
            result    = engine.evaluate_ha(target_df)

            buy      = result.get("buy")
            sell     = result.get("sell")
            conflict = result.get("conflict", False)

            # Use actual last candle time from market data, not current clock
            if "time" in target_df.columns and not target_df.empty:
                last_candle_ts = pd.Timestamp(target_df["time"].iloc[-1])
                if last_candle_ts.tzinfo is None:
                    last_candle_ts = last_candle_ts.tz_localize("UTC")
                last_candle_ts = last_candle_ts.tz_convert(tz)
            else:
                last_candle_ts = datetime.now(tz)

            if conflict:
                console.print(
                    f"[bold yellow]{sym:<12}[/bold yellow]  "
                    f"[yellow]⚡ CONFLICT — both BUY & SELL fired, both suppressed[/yellow]"
                )
                continue

            if buy and buy.is_valid:
                buy.timestamp = last_candle_ts
                print_buy_signal(buy)
                save_signal(buy)

            elif sell and sell.is_valid:
                sell.timestamp = last_candle_ts
                print_sell_signal(sell)
                save_signal(sell)

            else:
                pts_b = buy.points  if buy  else 0
                pts_s = sell.points if sell else 0
                hb    = buy.signals_in_history  if buy  else 0
                hs    = sell.signals_in_history if sell else 0

                # Last signal timestamp from the analysed data window
                buy_time  = buy.last_signal_time  if buy  and buy.last_signal_time  else None
                sell_time = sell.last_signal_time if sell and sell.last_signal_time else None

                if buy_time and sell_time:
                    last_sig = (f"[green]BUY[/green]  @ [yellow]{buy_time}[/yellow]"
                                if buy_time > sell_time
                                else f"[red]SELL[/red] @ [yellow]{sell_time}[/yellow]")
                elif buy_time:
                    last_sig = f"[green]BUY[/green]  @ [yellow]{buy_time}[/yellow]"
                elif sell_time:
                    last_sig = f"[red]SELL[/red] @ [yellow]{sell_time}[/yellow]"
                else:
                    last_sig = "[dim]No signal history yet[/dim]"

                hist_lbl = f"{days}d hist" if historical else "series"

                # pts colour
                buy_col  = "green"  if pts_b == 3 else ("yellow" if pts_b >= 2 else "dim")
                sell_col = "red"    if pts_s == 3 else ("yellow" if pts_s >= 2 else "dim")

                console.print(
                    f"[bold cyan]{sym:<12}[/bold cyan]  "
                    f"No signal  "
                    f"pts [bold {buy_col}]B:{pts_b}/3[/bold {buy_col}] "
                    f"[bold {sell_col}]S:{pts_s}/3[/bold {sell_col}]  "
                    f"[{hist_lbl}: [green]{hb} buy[/green] / [red]{hs} sell[/red]]  "
                    f"Last Signal: {last_sig}"
                )

        except KeyboardInterrupt:
            console.print("\n[yellow]Scan interrupted.[/]")
            break
        except Exception as e:
            console.print(f"  [red]{sym:<12}  error — {e}[/red]")

    console.rule(style="cyan")
    console.print(f"[dim]  Scan complete — {datetime.now(tz).strftime('%Y-%m-%d  %I:%M:%S %p %Z')}[/dim]\n")


# ══════════════════════════════════════════════════════════════════════════════
#  signals
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("signals")
@click.option("--history",    is_flag=True,   help="Show recent signal history")
@click.option("--open",       "show_open", is_flag=True, help="Show currently open positions")
@click.option("--type",       "sig_type",  default=None, type=click.Choice(["BUY","SELL"], case_sensitive=False))
@click.option("--ticker",     "-t", default=None, help="Filter by symbol")
@click.option("--date",       default=None, help="Filter by date (YYYY-MM-DD)")
@click.option("--limit",  "-n", default=50, show_default=True, help="Number of records to show")
def cmd_signals(history, show_open, sig_type, ticker, date, limit):
    """View signal history or open positions."""
    _imports()
    from src.data.db import get_recent_signals, get_open_trades
    from src.display.tables import print_signal_history, print_active_signals
    from src.signals.types import TradeRecord

    if show_open:
        rows = get_open_trades()
        # Reconstruct lightweight TradeRecord-like objects for display
        recs = []
        for r in rows:
            try:
                rec = TradeRecord(
                    trade_id=r["trade_id"], signal_type=r["signal_type"],
                    asset=r["asset"], timeframe=r["timeframe"],
                    entry_time=datetime.fromisoformat(r["entry_time"]),
                    entry_price=r["entry_price"], stop_loss_hard=r["stop_loss_hard"],
                    trailing_stop=r["trailing_stop"], position_size=r["position_size"],
                    account_risk_pct=r["account_risk_pct"],
                    alligator_point=bool(r.get("alligator_pt",0)),
                    stochastic_point=bool(r.get("stochastic_pt",0)),
                    vortex_point=bool(r.get("vortex_pt",0)),
                    jaw_at_entry=r.get("jaw_at_entry",0) or 0,
                    teeth_at_entry=r.get("teeth_at_entry",0) or 0,
                    lips_at_entry=r.get("lips_at_entry",0) or 0,
                )
                recs.append(rec)
            except Exception:
                pass
        if ticker:
            recs = [r for r in recs if r.asset.upper() == ticker.upper()]
        if recs:
            print_active_signals(recs)
        else:
            console.print("[dim]No open positions.[/]")
        return

    if history or not show_open:
        for stype in (["BUY", "SELL"] if not sig_type else [sig_type.upper()]):
            rows = get_recent_signals(stype, limit=limit)
            if ticker:
                rows = [r for r in rows if str(r.get("asset","")).upper() == ticker.upper()]
            if date:
                rows = [r for r in rows if str(r.get("timestamp","")).startswith(date)]
            if rows:
                print_signal_history(rows)
            else:
                console.print(f"[dim]No {stype} signals found.[/]")


# ══════════════════════════════════════════════════════════════════════════════
#  trades
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("trades")
@click.option("--log",   is_flag=True, help="Show full trade log")
@click.option("--today", is_flag=True, help="Show today's trades only")
@click.option("--pnl",   is_flag=True, help="Show PnL summary")
@click.option("--ticker", "-t", default=None, help="Filter by symbol")
@click.option("--limit", "-n", default=100, show_default=True)
def cmd_trades(log, today, pnl, ticker, limit):
    """View trade history and PnL summary."""
    _imports()
    from src.data.db import get_closed_trades
    from src.display.tables import print_pnl_summary

    rows = get_closed_trades(limit=limit)
    if ticker:
        rows = [r for r in rows if str(r.get("asset","")).upper() == ticker.upper()]
    if today:
        today_str = datetime.now().strftime("%Y-%m-%d")
        rows = [r for r in rows if (r.get("exit_time") or "").startswith(today_str)]

    if not rows:
        console.print("[dim]No closed trades found.[/]")
        return

    if pnl or not log:
        print_pnl_summary(rows)

    if log:
        tbl = Table(title="Trade Log", box=box.HEAVY_HEAD, show_lines=True)
        tbl.add_column("ID",      width=8)
        tbl.add_column("Type",    width=5)
        tbl.add_column("Asset",   width=10)
        tbl.add_column("Entry",   width=17)
        tbl.add_column("Exit",    width=17)
        tbl.add_column("Entry $", width=12, justify="right")
        tbl.add_column("Exit $",  width=12, justify="right")
        tbl.add_column("PnL %",   width=10, justify="right")
        tbl.add_column("Reason",  width=14)
        for r in rows:
            pnl_v   = float(r.get("pnl_pct", 0))
            pnl_str = f"{pnl_v:+.2f}%"
            col     = f"[green]{pnl_str}[/]" if pnl_v >= 0 else f"[red]{pnl_str}[/]"
            t_col   = "[green]BUY[/]" if r.get("signal_type") == "BUY" else "[red]SELL[/]"
            tbl.add_row(
                str(r.get("trade_id",""))[:8],
                t_col,
                str(r.get("asset","")),
                str(r.get("entry_time",""))[:16],
                str(r.get("exit_time",""))[:16],
                f"{r.get('entry_price',0):.5f}",
                f"{r.get('exit_price',0):.5f}" if r.get("exit_price") else "—",
                col,
                str(r.get("close_reason","")).replace("_"," ").title(),
            )
        console.print(tbl)


# ══════════════════════════════════════════════════════════════════════════════
#  backtest
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("backtest")
@click.option("--ticker",    "-t",  default=None,          help="Symbol to backtest")
@click.option("--all",       "bt_all", is_flag=True,       help="Backtest entire catalogue")
@click.option("--category",  "-c",  default=None,
              type=click.Choice(["forex","crypto","stocks","commodities","indices"], case_sensitive=False))
@click.option("--timeframe", "-tf", default="1h",          show_default=True)
@click.option("--days",      "-d",  default=180,           show_default=True)
@click.option("--balance",   "-b",  default=10000.0,       show_default=True, help="Starting balance")
@click.option("--csv",       is_flag=True,                 help="Export trade ledger to CSV")
@click.option("--ml",        is_flag=True,                 help="Apply ML filter during backtest")
def cmd_backtest(ticker, bt_all, category, timeframe, days, balance, csv, ml):
    """Run historical backtest and display performance report."""
    _imports()
    from src.backtest.backtester import Backtester
    from src.backtest.reporter   import print_trade_ledger, print_backtest_summary, export_csv
    from src.data.symbol_mapper  import get_all_symbols, get_symbols_by_class, best_source
    from src.data.db             import init_db

    init_db()

    if ticker:
        symbols = [ticker.upper()]
    elif category:
        symbols = get_symbols_by_class(category)
    elif bt_all:
        symbols = get_all_symbols()
    else:
        console.print("[red]Specify --ticker, --all, or --category[/]")
        raise SystemExit(1)

    engine = Backtester(account_balance=balance, use_ml=ml)
    start  = datetime.now(timezone.utc) - timedelta(days=days)

    for sym in symbols:
        console.rule(f"[cyan]{sym}[/]")
        trades = engine.run(
            symbol    = sym,
            timeframe = timeframe,
            start     = start,
            source    = best_source(sym),
        )
        print_trade_ledger(trades)
        print_backtest_summary(trades, sym, timeframe)
        if csv and trades:
            path = export_csv(trades, sym, timeframe)
            console.print(f"[dim]CSV saved: {path}[/]")


# ══════════════════════════════════════════════════════════════════════════════
#  assets
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("assets")
@click.option("--list",   "do_list",   is_flag=True, help="Show active asset catalogue")
@click.option("--add",                 default=None,  help="Add symbol  (e.g. XAUUSD)")
@click.option("--remove",              default=None,  help="Remove symbol")
@click.option("--category",  "-c",     default="forex", show_default=True,
              help="Category for --add")
def cmd_assets(do_list, add, remove, category):
    """Manage the asset universe scanned by the bot."""
    from src.data.symbol_mapper import ASSET_CATALOGUE, add_symbol, remove_symbol

    if add:
        add_symbol(add.upper(), category.lower())
        console.print(f"[green]Added {add.upper()} to {category}[/]")
        return

    if remove:
        remove_symbol(remove.upper())
        console.print(f"[yellow]Removed {remove.upper()}[/]")
        return

    # List
    tbl = Table(title="Asset Catalogue", box=box.SIMPLE_HEAD)
    tbl.add_column("Symbol",   style="cyan",   width=14)
    tbl.add_column("Class",    style="green",  width=14)
    tbl.add_column("Display",  style="dim",    width=20)
    tbl.add_column("Source",   width=12)

    for sym, meta in sorted(ASSET_CATALOGUE.items()):
        tbl.add_row(sym, meta.get("class",""), meta.get("display",""), meta.get("source",""))
    console.print(tbl)


# ══════════════════════════════════════════════════════════════════════════════
#  status
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("status")
@click.option("--risk", is_flag=True, help="Show current risk exposure breakdown")
def cmd_status(risk):
    """Show system health, API connectivity, and model status."""
    _imports()
    from src.ai.lm_studio_client  import LMStudioClient
    from src.ai.openrouter_client import OpenRouterClient
    from src.ml.model             import is_model_available
    from src.ml.train             import get_ml_status
    from src.data.db              import init_db, get_open_trades, get_closed_trades
    from src.display.tables       import print_status

    init_db()
    lm_ok  = LMStudioClient().is_available()
    or_ok  = OpenRouterClient().is_available()
    ml_st  = get_ml_status()

    open_trades  = get_open_trades()
    open_count   = len(open_trades)
    closed_count = len(get_closed_trades(limit=10000))

    from src.config import FINNHUB_API_KEY, SUPABASE_URL, ACCOUNT_BALANCE, TIMEZONE

    if risk:
        tbl = Table(title="Risk Exposure", box=box.HEAVY_HEAD, show_lines=True)
        tbl.add_column("Metric", style="dim", width=28)
        tbl.add_column("Value",  style="cyan", width=20)

        # Compute daily PnL from today's closed trades
        today_str = datetime.now().strftime("%Y-%m-%d")
        closed_today = [r for r in get_closed_trades(limit=10000)
                        if str(r.get("exit_time","")).startswith(today_str)]
        daily_pnl = sum(float(r.get("pnl", 0)) for r in closed_today)
        daily_pnl_pct = daily_pnl / ACCOUNT_BALANCE * 100 if ACCOUNT_BALANCE else 0

        # Total risk in open positions
        total_risk_pct = sum(float(r.get("account_risk_pct", 0)) for r in open_trades)

        from src.config import MAX_DAILY_DRAWDOWN, MAX_TRADES_PER_HOUR, MAX_RISK_PER_TRADE
        tbl.add_row("Account Balance",         f"${ACCOUNT_BALANCE:,.2f}")
        tbl.add_row("Open Positions",           str(open_count))
        tbl.add_row("Risk per Trade",           f"{MAX_RISK_PER_TRADE*100:.1f}%")
        tbl.add_row("Total Open Risk",          f"{total_risk_pct:.2f}%")
        tbl.add_row("Daily PnL",               f"${daily_pnl:+,.2f} ({daily_pnl_pct:+.2f}%)")
        tbl.add_row("Kill Switch Threshold",    f"{MAX_DAILY_DRAWDOWN*100:.0f}%")
        kill = "[red]ACTIVE[/]" if abs(daily_pnl_pct) >= MAX_DAILY_DRAWDOWN * 100 else "[green]OK[/]"
        tbl.add_row("Kill Switch Status",       kill)
        tbl.add_row("Max Trades/Hour",          str(MAX_TRADES_PER_HOUR))
        console.print(tbl)
        return

    print_status(
        lm_studio_ok     = lm_ok,
        openrouter_ok    = or_ok,
        finnhub_ok       = bool(FINNHUB_API_KEY),
        supabase_ok      = bool(SUPABASE_URL),
        ml_ready         = ml_st["model_available"],
        ml_samples       = ml_st["total_samples"],
        open_trades      = open_count,
        closed_trades    = closed_count,
        account_balance  = ACCOUNT_BALANCE,
        timezone         = TIMEZONE,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  start / stop
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("start")
@click.option("--timeframe",  "-tf",  default="1h",  show_default=True)
@click.option("--ticker",     "-t",   default=None,  help="Single ticker (omit for full catalogue)")
@click.option("--category",   "-c",   default=None,
              type=click.Choice(["forex","crypto","stocks","commodities","indices"], case_sensitive=False))
@click.option("--dry-run",    is_flag=True, default=True,  help="Signals only, no orders (default)")
@click.option("--live",       is_flag=True, default=False, help="LIVE mode — sends real orders")
@click.option("--broker",     default="auto", show_default=True,
              type=click.Choice(["auto", "alpaca", "kraken", "fxcm", "ibkr", "fp"], case_sensitive=False),
              help="Force execution through one broker. Use alpaca for Alpaca paper/live accounts.")
@click.option("--top",               default=10,    show_default=True)
@click.option("--balance",    "-b",  default=None,  type=float, help="Override account balance")
def cmd_start(timeframe, ticker, category, dry_run, live, broker, top, balance):
    """Start the continuous market scanner.

    Default is DRY RUN (signals only).  Use --live for real order submission.
    Press Ctrl+C to stop cleanly.
    """
    _imports()
    from src.data.db            import init_db
    from src.scanner.market_scanner import MarketScanner
    from src.data.symbol_mapper import get_symbols_by_class
    from src.scanner.asset_universe import get_enabled_symbols, get_entry
    from src.config             import ACCOUNT_BALANCE

    init_db()

    actual_dry_run = not live  # live flag overrides dry-run default
    selected_broker = None if str(broker).lower() == "auto" else str(broker).lower()
    if live:
        console.print(Panel(
            "[bold red]⚠  ORDER ROUTING ENABLED — The scanner will submit orders per TRADING_MODE and BrokerRouter[/]\n"
            "(Alpaca paper/live, IBKR, Kraken, or MT5). Press Ctrl+C to stop cleanly.",
            border_style="red",
        ))
    else:
        console.print(Panel("[bold green]DRY RUN — Signals will be displayed; no orders sent.[/]"))

    if ticker:
        symbols = [ticker.upper()]
    elif category:
        # Prefer enabled universe symbols for category scans so we don't
        # silently fall back to the legacy 31-symbol catalogue.
        universe_symbols = get_enabled_symbols()
        if category == "stocks":
            symbols = []
            for s in universe_symbols:
                entry = get_entry(s)
                if entry and entry.asset_class in ("stock", "etf"):
                    symbols.append(s)
        elif category == "commodities":
            symbols = []
            for s in universe_symbols:
                entry = get_entry(s)
                if entry and entry.asset_class == "commodity":
                    symbols.append(s)
        elif category == "indices":
            symbols = []
            for s in universe_symbols:
                entry = get_entry(s)
                if entry and entry.asset_class == "index":
                    symbols.append(s)
        elif category == "crypto":
            symbols = []
            for s in universe_symbols:
                entry = get_entry(s)
                if entry and entry.asset_class == "crypto":
                    symbols.append(s)
        elif category == "forex":
            symbols = []
            for s in universe_symbols:
                entry = get_entry(s)
                if entry and entry.asset_class == "forex":
                    symbols.append(s)
        else:
            symbols = get_symbols_by_class(category)

        # If a requested category doesn't exist in the enabled universe,
        # preserve backwards compatibility with legacy catalogue behavior.
        if not symbols:
            symbols = get_symbols_by_class(category)
    else:
        symbols = get_enabled_symbols()

    scanner = MarketScanner(
        symbols          = symbols,
        timeframe        = timeframe,
        top_candidates   = top,
        dry_run          = actual_dry_run,
        account_balance  = balance or ACCOUNT_BALANCE,
        execution_broker = selected_broker,
    )

    scanner.start()


@cli.command("stop")
def cmd_stop():
    """Write a stop signal for a running bot instance.

    This creates a 'stop.flag' file that the scanner checks each cycle.
    For immediate stop, press Ctrl+C on the running process.
    """
    Path("stop.flag").write_text("stop")
    console.print("[yellow]Stop flag written. The scanner will halt after the current cycle.[/]")


@cli.command("restart")
@click.pass_context
def cmd_restart(ctx):
    """Stop the current scanner and immediately restart it.

    Writes stop.flag, then invokes 'start' with the same defaults.
    """
    Path("stop.flag").write_text("stop")
    console.print("[yellow]Stop flag written. Restarting scanner...[/]")
    import time as _t
    _t.sleep(2)  # give the running scanner a moment to see the flag
    if Path("stop.flag").exists():
        Path("stop.flag").unlink()
    ctx.invoke(cmd_start)


@cli.command("pause")
@click.option("--ticker", "-t", required=True, help="Symbol to pause scanning on")
def cmd_pause(ticker):
    """Pause scanning for a specific ticker.

    Creates a pause flag file that the scanner checks before processing each symbol.
    Use 'assets --remove' + 'assets --add' to fully remove and re-add.
    """
    pause_dir = Path("data/pause_flags")
    pause_dir.mkdir(parents=True, exist_ok=True)
    (pause_dir / f"{ticker.upper()}.pause").write_text("paused")
    console.print(f"[yellow]Paused scanning for {ticker.upper()}. "
                  f"Delete data/pause_flags/{ticker.upper()}.pause to resume.[/]")


# ══════════════════════════════════════════════════════════════════════════════
#  ai
# ══════════════════════════════════════════════════════════════════════════════

@cli.group("ai")
def ai_group():
    """AI analysis commands (LM Studio + Kimi K2)."""
    pass


@ai_group.command("debrief")
@click.option("--days", "-d", default=7, show_default=True, help="Days of trades to summarise")
def ai_debrief(days):
    """Ask the AI to analyse recent trade performance and give suggestions."""
    _imports()
    from src.data.db import get_closed_trades
    from src.ai.signal_ranker import run_debrief

    rows = get_closed_trades(limit=500)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    recent = [r for r in rows if r.get("exit_time", "") >= cutoff.isoformat()[:10]]
    if not recent:
        console.print(f"[dim]No closed trades in the last {days} days.[/]")
        return

    wins   = sum(1 for r in recent if float(r.get("pnl_pct",0)) > 0)
    losses = len(recent) - wins
    avg_pnl= sum(float(r.get("pnl_pct",0)) for r in recent) / len(recent)
    total  = sum(float(r.get("pnl",0)) for r in recent)

    summary = (
        f"Trading performance summary — last {days} days:\n\n"
        f"Total trades: {len(recent)}\n"
        f"Winners: {wins}  Losers: {losses}  Win rate: {wins/len(recent)*100:.1f}%\n"
        f"Average PnL per trade: {avg_pnl:+.2f}%\n"
        f"Total PnL: ${total:+.2f}\n\n"
        f"Recent trades:\n"
    )
    for r in recent[:20]:
        summary += (
            f"  {r.get('signal_type','?')} {r.get('asset','')} "
            f"({r.get('close_reason','?')}) → {float(r.get('pnl_pct',0)):+.2f}%\n"
        )

    console.print(Panel("[dim]Querying AI...[/]", title="AI Debrief"))
    result = run_debrief(summary)
    if result:
        console.print(Panel(result, title="AI Debrief", border_style="cyan", expand=False))
    else:
        console.print("[red]AI is unavailable. Start LM Studio or configure OpenRouter.[/]")


# ══════════════════════════════════════════════════════════════════════════════
#  ml
# ══════════════════════════════════════════════════════════════════════════════

@cli.group("ml")
def ml_group():
    """ML model management commands."""
    pass


@ml_group.command("train")
@click.option("--force", is_flag=True, help="Train even if minimum data guards are not met")
def ml_train(force):
    """Train (or retrain) the false-signal filter model."""
    _imports()
    from src.ml.train import run_training, backfill_features_from_trades
    from src.data.db  import init_db

    init_db()
    console.print("[dim]Backfilling feature rows from closed trades...[/]")
    backfill_features_from_trades()

    console.print("[dim]Training local ML models (XGBoost + LightGBM)...[/]")
    result = run_training(force=force)
    if result["trained"]:
        metrics = result.get("metrics") or {}
        xgb_auc = ((metrics.get("xgboost") or {}).get("auc"))
        lgb_auc = ((metrics.get("lightgbm") or {}).get("auc"))
        chosen = metrics.get("chosen")
        console.print(Panel(
            f"[green]Model trained successfully![/]\n\n"
            f"Samples:   {result['n_samples']}\n"
            f"Wins:      {result.get('wins','?')}  Losses: {result.get('losses','?')}\n"
            f"XGB AUC:   {xgb_auc:.3f}" if xgb_auc is not None else f"XGB AUC:   N/A",
            title="ML Training",
            border_style="green",
        ))
        if lgb_auc is not None or chosen:
            console.print(Panel(
                f"LightGBM AUC: {lgb_auc:.3f}" if lgb_auc is not None else "LightGBM AUC: N/A",
                title=f"Chosen model: {chosen or 'xgboost'}",
                border_style="cyan",
            ))
    else:
        console.print(Panel(
            f"[yellow]{result['message']}[/]",
            title="ML Training",
            border_style="yellow",
        ))


@ml_group.command("status")
def ml_status():
    """Show current ML model status and training readiness."""
    _imports()
    from src.ml.train import get_ml_status
    from src.data.db  import init_db
    from src.display.tables import print_status

    init_db()
    st = get_ml_status()

    tbl = Table(title="ML Model Status", box=box.SIMPLE_HEAD)
    tbl.add_column("Property",  style="dim", width=24)
    tbl.add_column("Value",     style="cyan")

    tbl.add_row("Model trained",      "[green]Yes[/]" if st["model_available"] else "[red]No[/]")
    tbl.add_row("Total samples",      str(st["total_samples"]))
    tbl.add_row("Min needed",         str(st["min_samples_needed"]))
    tbl.add_row("Win rate in data",   f"{st['win_rate_in_data']*100:.1f}%")
    tbl.add_row("Confidence threshold", f"{st['threshold']*100:.0f}%")
    tbl.add_row("Ready to train",     "[green]Yes[/]" if st["ready_to_train"] else "[yellow]Need more trades[/]")

    console.print(tbl)


# ══════════════════════════════════════════════════════════════════════════════
#  scan-multitime
# ══════════════════════════════════════════════════════════════════════════════

@cli.command("scan-multitime")
@click.option("--ticker",     "-t",  default=None,  help="Symbol to scan  (e.g. EURUSD, BTC/USDT)")
@click.option("--all",        "scan_all", is_flag=True, help="Scan every asset in the catalogue")
@click.option("--category",   "-c",  default=None,
              type=click.Choice(["forex","crypto","stocks","commodities","indices"], case_sensitive=False),
              help="Scan all symbols in a category")
@click.option("--days",       "-d",  default=30,    show_default=True, help="Days of history to scan")
@click.option("--ohlc",       is_flag=True,          help="Use standard OHLC candles instead of Heikin Ashi")
def cmd_scan_multitime(ticker, scan_all, category, days, ohlc):
    """Scan 3m, 5m, 10m, 15m, 30m, 45m, 1h, 2h, 3h, 4h for signals for the last N days."""
    _imports()
    import pandas as pd
    from src.data.heikin_ashi      import convert_to_heikin_ashi
    from src.data.market_data      import get_historical_ohlcv, get_latest_candles
    from src.data.symbol_mapper    import get_all_symbols, get_symbols_by_class, best_source
    from src.signals.signal_engine import SignalEngine
    from src.display.tables        import print_buy_signal, print_sell_signal
    from src.data.db               import init_db, save_signal
    from src.scanner.candidate_ranker import score_symbol, rank_candidates

    init_db()

    # Updated timeframes to match TradingView menu (starting from 3m)
    timeframes = [
        "3m", "5m", "10m", "15m", "30m", "45m", "1h", "2h", "3h", "4h"
    ]

    # Build symbol list
    if ticker:
        symbols = [ticker.upper()]
    elif category:
        symbols = get_symbols_by_class(category)
    elif scan_all:
        symbols = get_all_symbols()
    else:
        console.print("[red]Specify --ticker, --all, or --category[/]")
        raise SystemExit(1)

    import pytz
    from src.config import TIMEZONE
    tz = pytz.timezone(TIMEZONE)

    for tf in timeframes:
        console.rule(f"[bold yellow]Timeframe: {tf}[/bold yellow]")
        for sym in symbols:
            try:
                start = datetime.now(timezone.utc) - timedelta(days=days)
                raw   = get_historical_ohlcv(sym, tf, start=start, source=best_source(sym))
                if raw.empty:
                    console.print(f"[dim]{sym:<12}[/dim]  [dim]no data[/dim]")
                    continue
                if ohlc:
                    target_df = raw
                else:
                    target_df = convert_to_heikin_ashi(raw)
                    # Overwrite base columns with HA values so indicators calculate using smoothed data
                    target_df["open"]  = target_df["ha_open"]
                    target_df["high"]  = target_df["ha_high"]
                    target_df["low"]   = target_df["ha_low"]
                    target_df["close"] = target_df["ha_close"]

                engine = SignalEngine(sym, tf)
                result = engine.evaluate_ha(target_df)
                buy  = result.get("buy")
                sell = result.get("sell")
                conflict = result.get("conflict", False)

                # Use actual last candle time from market data, not current clock
                if "time" in target_df.columns and not target_df.empty:
                    last_candle_ts = pd.Timestamp(target_df["time"].iloc[-1])
                    if last_candle_ts.tzinfo is None:
                        last_candle_ts = last_candle_ts.tz_localize("UTC")
                    last_candle_ts = last_candle_ts.tz_convert(tz)
                else:
                    last_candle_ts = datetime.now(tz)

                if conflict:
                    console.print(
                        f"[bold yellow]{sym:<12}[/bold yellow]  "
                        f"[yellow]⚡ CONFLICT — both BUY & SELL fired, both suppressed[/yellow]"
                    )
                    continue
                if buy and buy.is_valid:
                    buy.timestamp = last_candle_ts
                    print_buy_signal(buy)
                    save_signal(buy)
                elif sell and sell.is_valid:
                    sell.timestamp = last_candle_ts
                    print_sell_signal(sell)
                    save_signal(sell)
                else:
                    pts_b = buy.points  if buy  else 0
                    pts_s = sell.points if sell else 0
                    hb = buy.signals_in_history  if buy  else 0
                    hs = sell.signals_in_history if sell else 0
                    
                    # Format the last signal time from the evaluated results
                    last_time_str = "None"
                    buy_time = buy.last_signal_time if buy and buy.last_signal_time else None
                    sell_time = sell.last_signal_time if sell and sell.last_signal_time else None
                    
                    # pts colour
                    buy_col  = "green"  if pts_b == 3 else ("yellow" if pts_b >= 2 else "dim")
                    sell_col = "red"    if pts_s == 3 else ("yellow" if pts_s >= 2 else "dim")

                    if buy_time and sell_time:
                        last_sig = (f"[green]BUY[/green]  @ [yellow]{buy_time}[/yellow]"
                                    if buy_time > sell_time
                                    else f"[red]SELL[/red] @ [yellow]{sell_time}[/yellow]")
                    elif buy_time:
                        last_sig = f"[green]BUY[/green]  @ [yellow]{buy_time}[/yellow]"
                    elif sell_time:
                        last_sig = f"[red]SELL[/red] @ [yellow]{sell_time}[/yellow]"
                    else:
                        last_sig = "[dim]No signal history yet[/dim]"

                    console.print(
                        f"[bold cyan]{sym:<12}[/bold cyan]  "
                        f"No signal  "
                        f"pts [bold {buy_col}]B:{pts_b}/3[/bold {buy_col}] "
                        f"[bold {sell_col}]S:{pts_s}/3[/bold {sell_col}]  "
                        f"[{days}d hist: [green]{hb} buy[/green] / [red]{hs} sell[/red]]  "
                        f"Last Signal: {last_sig}"
                    )

            except KeyboardInterrupt:
                console.print("\n[yellow]Scan interrupted.[/]")
                break
            except Exception as e:
                console.print(f"[red]  {sym}: error — {e}[/]")

# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cli()
