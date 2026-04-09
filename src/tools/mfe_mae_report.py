"""MFE / MAE fleet report — Phase 7.

Analyses all closed trades and flags three categories of exit-quality problems:

* **high_mfe_poor_pnl** — trade reached a significant peak but exited with
  little profit (capture ratio below floor).
* **giveback_after_protection** — profit-protection was armed but the trade
  still gave back to breakeven or a loss.
* **never_protected** — trade closed without any protection mechanism ever
  activating (break-even, profit-lock, or trailing TP stage-1).

Usage::

    from src.tools.mfe_mae_report import get_mfe_mae_report_data, print_mfe_mae_report
    data = get_mfe_mae_report_data()          # uses default db path
    print_mfe_mae_report(data)
    # or just the dict if you want to process it programmatically:
    data["summary"]
    data["high_mfe_poor_pnl"]
    data["giveback_after_protection"]
    data["never_protected"]
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── Flag thresholds ───────────────────────────────────────────────────────────

MFE_POOR_PNL_THRESHOLD   = 1.5    # MFE % that qualifies a trade as "had a real peak"
MFE_CAPTURE_FLOOR        = 0.35   # pnl_pct must be ≥ mfe * this ratio or it is flagged
PROTECTION_GIVEBACK_FLOOR = 0.25  # after protection armed, pnl must be ≥ mfe * this or flagged


# ── Core analytics ────────────────────────────────────────────────────────────

def analyze_mfe_mae(trades: list[dict]) -> dict:
    """Classify closed trades into three exit-quality flag buckets.

    Parameters
    ----------
    trades:
        List of trade dicts as returned by ``get_closed_trades()``.  Expected
        fields (all may be ``None`` on older rows):

        * ``max_unrealized_profit`` — peak unrealised gain (%) during the trade
        * ``pnl_pct``               — realised P&L (%)
        * ``was_protected_profit``  — bool / 0 / 1
        * ``break_even_armed``      — bool / 0 / 1
        * ``profit_lock_stage``     — integer stage (0 = no lock)

    Returns
    -------
    dict containing:

    * ``"high_mfe_poor_pnl"``         — list[dict] of flagged trades
    * ``"giveback_after_protection"`` — list[dict] of flagged trades
    * ``"never_protected"``           — list[dict] of flagged trades
    * ``"summary"``                   — aggregate stats dict
    """
    high_mfe_poor_pnl:         list[dict] = []
    giveback_after_protection: list[dict] = []
    never_protected:           list[dict] = []

    for t in trades:
        mfe        = float(t.get("max_unrealized_profit") or 0.0)
        pnl        = float(t.get("pnl_pct")               or 0.0)
        protected  = bool(t.get("was_protected_profit")    or False)
        be_armed   = bool(t.get("break_even_armed")        or False)
        pl_stage   = int( t.get("profit_lock_stage")       or 0)

        # Flag 1 — high MFE but poor captured P&L
        if mfe > MFE_POOR_PNL_THRESHOLD:
            capture_ratio = pnl / mfe if mfe != 0 else 0.0
            if capture_ratio < MFE_CAPTURE_FLOOR:
                high_mfe_poor_pnl.append({**t, "_capture_ratio": round(capture_ratio, 4)})

        # Flag 2 — protection armed but trade gave most of the gain back
        if protected:
            if pnl < 0 or (mfe > 0 and pnl / mfe < PROTECTION_GIVEBACK_FLOOR):
                giveback_after_protection.append({**t, "_capture_ratio": round(pnl / mfe, 4) if mfe else None})

        # Flag 3 — no protection mechanism ever activated
        if not protected and not be_armed and pl_stage == 0:
            never_protected.append(t)

    total = len(trades)
    summary = {
        "total_closed":                  total,
        "high_mfe_poor_pnl_count":       len(high_mfe_poor_pnl),
        "giveback_after_protection_count": len(giveback_after_protection),
        "never_protected_count":         len(never_protected),
        "high_mfe_poor_pnl_rate":        round(len(high_mfe_poor_pnl)          / total, 4) if total else 0.0,
        "giveback_after_protection_rate": round(len(giveback_after_protection)  / total, 4) if total else 0.0,
        "never_protected_rate":          round(len(never_protected)             / total, 4) if total else 0.0,
    }
    return {
        "high_mfe_poor_pnl":         high_mfe_poor_pnl,
        "giveback_after_protection": giveback_after_protection,
        "never_protected":           never_protected,
        "summary":                   summary,
    }


def get_mfe_mae_report_data(db_path=None) -> dict:
    """Pull closed trades from the database and run :func:`analyze_mfe_mae`.

    Parameters
    ----------
    db_path:
        SQLite path.  Defaults to ``src.config.SQLITE_PATH``.
    """
    if db_path is None:
        try:
            from src.config import SQLITE_PATH
            db_path = SQLITE_PATH
        except ImportError:
            db_path = "data/algobot.db"

    from src.data.db import get_closed_trades
    trades = get_closed_trades(limit=10_000)
    return analyze_mfe_mae(trades)


# ── Rich reporting ────────────────────────────────────────────────────────────

def _pct(val) -> str:
    """Format a float as a signed percentage string, or '—' if None."""
    if val is None:
        return "—"
    return f"{float(val):+.2f}%"


def print_mfe_mae_report(data: dict) -> None:
    """Render the three flag tables and a summary panel using Rich.

    Parameters
    ----------
    data:
        Dict as returned by :func:`analyze_mfe_mae` or
        :func:`get_mfe_mae_report_data`.
    """
    try:
        from rich import box
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        _print_mfe_mae_report_plain(data)
        return

    console = Console()
    summary = data.get("summary", {})

    # ── Summary banner ────────────────────────────────────────────────────────
    total  = summary.get("total_closed", 0)
    flag1n = summary.get("high_mfe_poor_pnl_count", 0)
    flag2n = summary.get("giveback_after_protection_count", 0)
    flag3n = summary.get("never_protected_count", 0)
    f1r    = summary.get("high_mfe_poor_pnl_rate",          0.0)
    f2r    = summary.get("giveback_after_protection_rate",   0.0)
    f3r    = summary.get("never_protected_rate",             0.0)

    banner = (
        f"Total closed: [bold]{total}[/bold]   "
        f"Flag-1 (high-MFE/poor-PnL): [yellow]{flag1n}[/yellow] ({f1r*100:.1f}%)   "
        f"Flag-2 (gave-back after protection): [red]{flag2n}[/red] ({f2r*100:.1f}%)   "
        f"Flag-3 (never-protected): [dim]{flag3n}[/dim] ({f3r*100:.1f}%)"
    )
    console.print(Panel(banner, title="[bold]MFE / MAE Fleet Report[/bold]", expand=False))

    # ── Shared column builder ─────────────────────────────────────────────────
    def _build_table(title: str, rows: list[dict], extra_col: Optional[str] = None) -> Table:
        tbl = Table(
            title=title,
            box=box.HEAVY_HEAD,
            style="white on grey7",
            header_style="bold bright_cyan on grey15",
            show_lines=False,
            expand=False,
        )
        tbl.add_column("Trade ID",    style="dim",        no_wrap=True)
        tbl.add_column("Asset",       style="bold white")
        tbl.add_column("Mode",        style="cyan")
        tbl.add_column("MFE %",       style="green",      justify="right")
        tbl.add_column("PnL %",       style="yellow",     justify="right")
        if extra_col:
            tbl.add_column(extra_col, style="bright_white", justify="right")
        tbl.add_column("BE Armed",    justify="center")
        tbl.add_column("PL Stage",    justify="center")
        tbl.add_column("Protected",   justify="center")

        for r in rows:
            trade_id  = str(r.get("trade_id",  "?"))[:12]
            asset     = str(r.get("asset",     "?"))
            mode      = str(r.get("strategy_mode", "?"))
            mfe_s     = _pct(r.get("max_unrealized_profit"))
            pnl_s     = _pct(r.get("pnl_pct"))
            be_armed  = "✓" if r.get("break_even_armed")   else "·"
            pl_stage  = str(r.get("profit_lock_stage") or 0)
            protected = "✓" if r.get("was_protected_profit") else "·"
            row_data  = [trade_id, asset, mode, mfe_s, pnl_s]
            if extra_col:
                cap_r = r.get("_capture_ratio")
                row_data.append(f"{cap_r:.2f}" if cap_r is not None else "—")
            row_data.extend([be_armed, pl_stage, protected])
            tbl.add_row(*row_data)
        return tbl

    # ── Flag 1: High MFE / Poor PnL ──────────────────────────────────────────
    rows1 = data.get("high_mfe_poor_pnl", [])
    if rows1:
        console.print(_build_table(
            f"Flag 1 — High MFE / Poor Captured PnL  "
            f"[MFE > {MFE_POOR_PNL_THRESHOLD}%  and  capture < {MFE_CAPTURE_FLOOR:.0%}]",
            rows1, extra_col="Capture Ratio",
        ))
    else:
        console.print(f"[green]Flag 1 — No high-MFE/poor-PnL trades.[/green]")

    # ── Flag 2: Gave Back After Protection ───────────────────────────────────
    rows2 = data.get("giveback_after_protection", [])
    if rows2:
        console.print(_build_table(
            f"Flag 2 — Giveback After Protection Armed",
            rows2, extra_col="Capture Ratio",
        ))
    else:
        console.print("[green]Flag 2 — No giveback-after-protection trades.[/green]")

    # ── Flag 3: Never Protected ───────────────────────────────────────────────
    rows3 = data.get("never_protected", [])
    if rows3:
        console.print(_build_table(
            "Flag 3 — Never-Protected Trades  [no BE / PL / trailing-TP]",
            rows3,
        ))
    else:
        console.print("[green]Flag 3 — All closed trades had at least one protection mechanism.[/green]")


def _print_mfe_mae_report_plain(data: dict) -> None:
    """Fallback plain-text report when Rich is unavailable."""
    s = data.get("summary", {})
    print("=== MFE/MAE Fleet Report ===")
    print(f"  Total closed        : {s.get('total_closed', 0)}")
    print(f"  Flag-1 high-MFE/poor: {s.get('high_mfe_poor_pnl_count', 0)}")
    print(f"  Flag-2 gave-back    : {s.get('giveback_after_protection_count', 0)}")
    print(f"  Flag-3 never-prot.  : {s.get('never_protected_count', 0)}")
    for key in ("high_mfe_poor_pnl", "giveback_after_protection", "never_protected"):
        print(f"\n--- {key} ---")
        for row in data.get(key, []):
            print(f"  {row.get('trade_id','?')} | {row.get('asset','?')} | "
                  f"MFE={row.get('max_unrealized_profit','?')} PnL={row.get('pnl_pct','?')}")
