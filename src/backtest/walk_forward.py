"""Walk-forward validation engine.

Splits historical data into rolling train/test windows and runs
the backtester on each, collecting per-period metrics to detect
overfitting or strategy instability.

Usage:
    from src.backtest.walk_forward import run_walk_forward
    results = run_walk_forward("EURUSD", "1h", train_months=12, test_months=3)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.backtest.backtester import Backtester
from src.backtest.reporter   import _compute_metrics
from src.data.symbol_mapper  import best_source

log = logging.getLogger(__name__)


def run_walk_forward(
    symbol:       str,
    timeframe:    str,
    total_days:   int   = 730,
    train_months: int   = 12,
    test_months:  int   = 3,
    step_months:  int   = 3,
    balance:      float = 10_000.0,
    use_ml:       bool  = False,
) -> list[dict]:
    """Run rolling walk-forward validation.

    Parameters
    ----------
    symbol        : Symbol to test.
    timeframe     : Candle timeframe.
    total_days    : Total history to cover.
    train_months  : Training window length.
    test_months   : Test window length.
    step_months   : How far to roll forward each iteration.
    balance       : Starting account balance.
    use_ml        : Whether to apply ML filter.

    Returns
    -------
    List of dicts, one per walk-forward period, each with:
        period, train_start, train_end, test_start, test_end,
        train_metrics, test_metrics
    """
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=total_days)

    train_delta = timedelta(days=train_months * 30)
    test_delta  = timedelta(days=test_months  * 30)
    step_delta  = timedelta(days=step_months  * 30)

    results: list[dict] = []
    period = 0
    cursor = start

    source = best_source(symbol)

    while cursor + train_delta + test_delta <= now:
        period += 1
        train_start = cursor
        train_end   = cursor + train_delta
        test_start  = train_end
        test_end    = test_start + test_delta

        log.info("Walk-forward %d: train %s → %s | test %s → %s",
                 period, train_start.date(), train_end.date(),
                 test_start.date(), test_end.date())

        # Train period
        bt_train = Backtester(account_balance=balance, use_ml=use_ml)
        train_trades = bt_train.run(symbol, timeframe, start=train_start,
                                     end=train_end, source=source)
        train_m = _compute_metrics(train_trades) if train_trades else _empty_metrics()

        # Test period
        bt_test = Backtester(account_balance=balance, use_ml=use_ml)
        test_trades = bt_test.run(symbol, timeframe, start=test_start,
                                   end=test_end, source=source)
        test_m = _compute_metrics(test_trades) if test_trades else _empty_metrics()

        results.append({
            "period":        period,
            "train_start":   train_start.isoformat(),
            "train_end":     train_end.isoformat(),
            "test_start":    test_start.isoformat(),
            "test_end":      test_end.isoformat(),
            "train_trades":  len(train_trades),
            "test_trades":   len(test_trades),
            "train_metrics": train_m,
            "test_metrics":  test_m,
        })

        cursor += step_delta

    return results


def _empty_metrics() -> dict:
    return {
        "total_trades": 0,
        "win_rate":     0.0,
        "profit_factor":0.0,
        "max_drawdown": 0.0,
        "sharpe":       0.0,
        "total_pnl":    0.0,
    }


def print_walk_forward_report(results: list[dict], symbol: str) -> None:
    """Pretty-print the walk-forward results using Rich tables."""
    from rich.console import Console
    from rich.table   import Table
    from rich         import box

    console = Console()

    if not results:
        console.print("[dim]No walk-forward periods generated.[/]")
        return

    tbl = Table(
        title=f"Walk-Forward Validation — {symbol}",
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    tbl.add_column("#",            width=4, justify="right")
    tbl.add_column("Train Period", width=24)
    tbl.add_column("Test Period",  width=24)
    tbl.add_column("Train Trades", width=8, justify="right")
    tbl.add_column("Test Trades",  width=8, justify="right")
    tbl.add_column("Train Win%",   width=10, justify="right")
    tbl.add_column("Test Win%",    width=10, justify="right")
    tbl.add_column("Train PF",     width=8, justify="right")
    tbl.add_column("Test PF",      width=8, justify="right")
    tbl.add_column("Test DD%",     width=8, justify="right")

    for r in results:
        tm = r["train_metrics"]
        ts = r["test_metrics"]
        tbl.add_row(
            str(r["period"]),
            f"{r['train_start'][:10]} → {r['train_end'][:10]}",
            f"{r['test_start'][:10]} → {r['test_end'][:10]}",
            str(r["train_trades"]),
            str(r["test_trades"]),
            f"{tm['win_rate']*100:.1f}%",
            f"{ts['win_rate']*100:.1f}%",
            f"{tm['profit_factor']:.2f}",
            f"{ts['profit_factor']:.2f}",
            f"{ts['max_drawdown']*100:.2f}%",
        )

    console.print(tbl)
