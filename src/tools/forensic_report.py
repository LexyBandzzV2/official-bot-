"""Single-trade forensic report — Phase 3.

Fetches all lifecycle data for one trade and produces a structured diagnosis
with output in terminal text, Markdown, or JSON.

CLI usage::

    python -m src.tools.forensic_report <trade_id> [--format text|md|json]

Programmatic usage::

    from src.tools.forensic_report import generate_report, format_text
    report = generate_report("some-trade-uuid")
    print(format_text(report))
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

# ─── diagnosis category constants ─────────────────────────────────────────────
# Phase 9 label strings — exact user-specified display names
DIAG_MISSING_LOGGING        = "missing logging"
DIAG_WRONG_EXIT_POLICY      = "wrong exit policy for mode"
DIAG_TRAIL_NEVER_ARMED      = "trailing never properly armed"
DIAG_GIVEBACK_TOO_LOOSE     = "peak-giveback exit was too loose"
DIAG_PROTECTION_TOO_LATE    = "protected_profit_activated_too_late"
DIAG_WEAK_ENTRY             = "weak entry"
DIAG_STRONG_ENTRY_WEAK_EXIT = "strong entry, weak exit"
DIAG_CLEAN                  = "CLEAN"

# Backward-compat alias (Phase 3 tests import DIAG_MISSING_COVERAGE)
DIAG_MISSING_COVERAGE       = DIAG_MISSING_LOGGING

# Priority order used by primary_diagnosis() — earlier entries win
_DIAG_PRIORITY = [
    DIAG_MISSING_LOGGING,
    DIAG_WRONG_EXIT_POLICY,
    DIAG_TRAIL_NEVER_ARMED,
    DIAG_GIVEBACK_TOO_LOOSE,
    DIAG_PROTECTION_TOO_LATE,
    DIAG_WEAK_ENTRY,
    DIAG_STRONG_ENTRY_WEAK_EXIT,
]


def _pct(v: Any) -> float:
    """Coerce a value to float, returning 0.0 on None/error."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ts(v: Any) -> str:
    """Return a short human-readable timestamp string, or '-' if absent."""
    if not v:
        return "-"
    try:
        return str(v).replace("T", " ").split(".")[0]
    except Exception:
        return str(v)


def _indicator_count(entry_reason: str | None) -> int:
    """Count how many indicator names appear in an entry_reason string."""
    if not entry_reason:
        return 0
    flags = entry_reason.split("|")[0]
    return sum(1 for name in ("alligator", "stochastic", "vortex") if name in flags)


# ─── diagnosis engine ─────────────────────────────────────────────────────────

def diagnose(trade: dict, events: list[dict]) -> list[str]:
    """Return a list of diagnosis category strings for this trade.

    An empty list means no issues detected (CLEAN).
    """
    results: list[str] = []

    mfe       = _pct(trade.get("max_unrealized_profit"))
    mae       = _pct(trade.get("min_unrealized_profit"))
    pnl_pct   = _pct(trade.get("pnl_pct"))
    reason    = trade.get("close_reason") or ""
    entry_rsn = trade.get("entry_reason")
    was_protected = bool(trade.get("was_protected_profit"))

    # 1. Missing logging — trade recorded before Phase 3 fields or no lifecycle data at all
    if (
        entry_rsn is None
        and mfe == 0.0
        and mae == 0.0
        and not any(e["event_type"] in ("mfe_update", "mae_update") for e in events)
    ):
        results.append(DIAG_MISSING_LOGGING)
        return results  # further diagnoses would be unreliable without data

    # 2. Wrong exit policy for mode (Phase 9)
    _mode            = trade.get("strategy_mode", "UNKNOWN")
    _used_fallback   = bool(trade.get("used_fallback_policy", 0))
    _exit_policy_lc  = (trade.get("exit_policy_name") or "").lower()
    _wrong_policy    = False
    if _mode == "SCALP" and reason == "ALLIGATOR_TP":
        # Alligator TP is a trend-following exit designed for SWING/INTERMEDIATE
        _wrong_policy = True
    elif _used_fallback and _exit_policy_lc:
        if _mode == "SCALP" and ("swing" in _exit_policy_lc or "intermediate" in _exit_policy_lc):
            _wrong_policy = True
        elif _mode == "SWING" and "scalp" in _exit_policy_lc:
            _wrong_policy = True
    if _wrong_policy:
        results.append(DIAG_WRONG_EXIT_POLICY)

    # 3. Weak entry — few indicators fired OR MFE tiny
    if _indicator_count(entry_rsn) < 2 or mfe < 0.5:
        results.append(DIAG_WEAK_ENTRY)

    # 4. Peak giveback too loose — exited via PEAK_GIVEBACK_EXIT but captured < 30% of MFE
    if reason == "PEAK_GIVEBACK_EXIT" and mfe > 1.0:
        capture_ratio = pnl_pct / mfe if mfe > 0 else 0.0
        if capture_ratio < 0.30:
            results.append(DIAG_GIVEBACK_TOO_LOOSE)

    # 5. Trail never armed properly — only the initial_stop event, no subsequent trail moves
    trail_events = [e for e in events if e.get("event_type") == "trail_update"]
    if not any(e.get("trail_update_reason") != "initial_stop" for e in trail_events):
        results.append(DIAG_TRAIL_NEVER_ARMED)

    # 6. Protected profit activated too late
    if was_protected:
        entry_t_str = trade.get("entry_time")
        exit_t_str  = trade.get("exit_time")
        be_events   = [e for e in events if e.get("event_type") == "break_even_armed"]
        if entry_t_str and exit_t_str and be_events and pnl_pct < 0:
            try:
                entry_t = datetime.fromisoformat(str(entry_t_str).rstrip("Z"))
                exit_t  = datetime.fromisoformat(str(exit_t_str).rstrip("Z"))
                be_t    = datetime.fromisoformat(str(be_events[0]["event_time"]).rstrip("Z"))
                duration = (exit_t - entry_t).total_seconds()
                if duration > 0:
                    activation_frac = (be_t - entry_t).total_seconds() / duration
                    if activation_frac >= 0.80:
                        results.append(DIAG_PROTECTION_TOO_LATE)
            except (ValueError, TypeError):
                pass

    # 7. Strong entry, weak exit — MFE was good but final pnl low
    if mfe > 1.0 and pnl_pct < mfe * 0.30 and reason != "HARD_STOP":
        results.append(DIAG_STRONG_ENTRY_WEAK_EXIT)

    return results


def primary_diagnosis(diags: list[str]) -> str:
    """Return the single highest-priority diagnosis from a list returned by diagnose().

    Uses ``_DIAG_PRIORITY`` ordering — first match wins.  Returns ``DIAG_CLEAN``
    when the list is empty or contains no recognised label.
    """
    for label in _DIAG_PRIORITY:
        if label in diags:
            return label
    return DIAG_CLEAN


# ─── report assembly ──────────────────────────────────────────────────────────

def generate_report(trade_id: str) -> dict:
    """Fetch trade + lifecycle events and assemble the full forensic report dict."""
    try:
        from src.data.db import get_trade_forensic
        forensic = get_trade_forensic(trade_id)
    except Exception as e:
        return {"error": str(e), "trade_id": trade_id}

    trade  = forensic.get("trade")
    events = forensic.get("events", [])

    if trade is None:
        return {"error": f"Trade not found: {trade_id}", "trade_id": trade_id}

    diags = diagnose(trade, events)

    # Build trail history list for display
    trail_history = [
        {
            "time":   e.get("event_time"),
            "type":   e.get("event_type"),
            "reason": e.get("trail_update_reason"),
            "old":    e.get("old_value"),
            "new":    e.get("new_value"),
            "price":  e.get("current_price"),
            "stage":  e.get("profit_lock_stage"),
        }
        for e in events
        if e.get("event_type") in (
            "trail_update", "break_even_armed", "profit_lock_stage"
        )
    ]

    # Profit-lock stage progression — ordered snapshots of stage transitions
    profit_lock_stage_progression = [
        {
            "time":       e.get("event_time"),
            "stage":      e.get("profit_lock_stage", 0),
            "event_type": e.get("event_type"),
        }
        for e in events
        if e.get("event_type") in ("profit_lock_stage", "break_even_armed", "trail_update")
        and e.get("profit_lock_stage") is not None
    ]

    # MFE / MAE event timestamps
    mfe_events = [e for e in events if e.get("event_type") == "mfe_update"]
    mae_events = [e for e in events if e.get("event_type") == "mae_update"]

    return {
        "trade_id": trade_id,
        # Identity
        "asset":           trade.get("asset"),
        "timeframe":       trade.get("timeframe"),
        "strategy_mode":   trade.get("strategy_mode"),
        "entry_reason":    trade.get("entry_reason"),
        # Sides
        "expected_direction":  trade.get("signal_type"),
        "actual_order_side":   trade.get("signal_type"),
        "initial_exit_policy": trade.get("initial_exit_policy"),
        # Stops
        "initial_stop_value":             trade.get("initial_stop_value"),
        "break_even_armed":               bool(trade.get("break_even_armed")),
        "protected_profit_activation_time": _ts(trade.get("protected_profit_activation_time")),
        # Excursion
        "max_unrealized_profit": _pct(trade.get("max_unrealized_profit")),
        "timestamp_of_mfe":      _ts(mfe_events[-1]["event_time"] if mfe_events else trade.get("timestamp_of_mfe")),
        "min_unrealized_profit": _pct(trade.get("min_unrealized_profit")),
        "timestamp_of_mae":      _ts(mae_events[-1]["event_time"] if mae_events else trade.get("timestamp_of_mae")),
        # Lock progression
        "profit_lock_stage":   trade.get("profit_lock_stage", 0),
        "was_protected_profit":bool(trade.get("was_protected_profit")),
        # Trail history
        "trail_history": trail_history,
        # Exit
        "exit_policy_name": trade.get("exit_policy_name"),
        "exit_reason":      trade.get("close_reason"),
        "realized_pnl":     trade.get("pnl"),
        "realized_pnl_pct": trade.get("pnl_pct"),
        "entry_time":       _ts(trade.get("entry_time")),
        "exit_time":        _ts(trade.get("exit_time")),
        # Phase 9: lock progression and primary diagnosis
        "profit_lock_stage_progression": profit_lock_stage_progression,
        "primary_diagnosis":             primary_diagnosis(diags),
        # Raw
        "_diagnosis":  diags,
        "_all_events": events,
    }


# ─── Rich terminal output (Phase 9) ──────────────────────────────────────────

def print_forensic_report(report: dict, *, console: "Any" = None) -> None:
    """Rich terminal output for a forensic trade report.

    Renders three sections:
    1. Identity & Entry table
    2. Lifecycle & Exit table
    3. Trailing stop history table (omitted when empty)
    4. Diagnosis panel
    """
    try:
        from rich.console import Console as _Console
        from rich import box as _box
        from rich.table import Table as _Table
        from rich.panel import Panel as _Panel
        from rich.text import Text as _Text
        _rich_ok = True
    except ImportError:
        _rich_ok = False

    if not _rich_ok:
        print(format_text(report))
        return

    con = console or _Console()

    if "error" in report:
        con.print(f"[bold red]FORENSIC ERROR:[/] {report['error']}")
        return

    diags   = report.get("_diagnosis") or []
    primary = report.get("primary_diagnosis") or primary_diagnosis(diags)

    _BOX   = _box.HEAVY_HEAD
    _STYLE = "white on grey7"
    _HEAD  = "bold bright_cyan on grey15"

    # ── Table 1: Identity & Entry ──────────────────────────────────────────
    t1 = _Table(box=_BOX, style=_STYLE, header_style=_HEAD, show_header=False,
                title=f"[bold]Trade Forensic — {report.get('trade_id', '?')}[/]")
    t1.add_column("Field",  style="bold dim", no_wrap=True)
    t1.add_column("Value",  style="white")

    t1.add_row("Asset",               str(report.get("asset") or "—"))
    t1.add_row("Timeframe",           str(report.get("timeframe") or "—"))
    t1.add_row("Strategy Mode",       str(report.get("strategy_mode") or "—"))
    t1.add_row("Direction",           str(report.get("expected_direction") or "—"))
    t1.add_row("Actual Order Side",   str(report.get("actual_order_side") or "—"))
    t1.add_row("Entry Reason",        str(report.get("entry_reason") or "—"))
    t1.add_row("Entry Reason Code",   str(report.get("entry_reason_code") or "—"))
    t1.add_row("Initial Exit Policy", str(report.get("initial_exit_policy") or "—"))
    t1.add_row("Entry Time",          str(report.get("entry_time") or "—"))
    t1.add_row("Initial Stop",        str(report.get("initial_stop_value") or "—"))

    con.print(t1)

    # ── Table 2: Lifecycle & Exit ──────────────────────────────────────────
    mfe = report.get("max_unrealized_profit", 0.0) or 0.0
    mae = report.get("min_unrealized_profit", 0.0) or 0.0
    pnl = report.get("realized_pnl_pct", 0.0) or 0.0

    def _clr(v: float) -> str:
        return "green" if v > 0 else ("red" if v < 0 else "white")

    t2 = _Table(box=_BOX, style=_STYLE, header_style=_HEAD, show_header=False)
    t2.add_column("Field",  style="bold dim", no_wrap=True)
    t2.add_column("Value",  style="white")

    t2.add_row("Break-even Armed",
               "[green]Yes[/]" if report.get("break_even_armed") else "[red]No[/]")
    t2.add_row("Profit Lock Stage",   str(report.get("profit_lock_stage", 0)))
    t2.add_row("MFE",  _Text(f"{mfe:+.3f}%", style=_clr(mfe)) )
    t2.add_row("MAE",  _Text(f"{mae:+.3f}%", style=_clr(mae)) )
    t2.add_row("MFE Timestamp",       str(report.get("timestamp_of_mfe") or "—"))
    t2.add_row("MAE Timestamp",       str(report.get("timestamp_of_mae") or "—"))
    t2.add_row("Exit Policy Name",    str(report.get("exit_policy_name") or "—"))
    t2.add_row("Exit Reason",         str(report.get("exit_reason") or "—"))
    t2.add_row("Exit Time",           str(report.get("exit_time") or "—"))
    t2.add_row("Realized PnL",        _Text(f"{pnl:+.3f}%", style=_clr(pnl)))
    t2.add_row("Was Protected Profit",
               "[green]Yes[/]" if report.get("was_protected_profit") else "[red]No[/]")
    t2.add_row("BE Activation Time",
               str(report.get("protected_profit_activation_time") or "—"))

    con.print(t2)

    # ── Table 3: Trail history (optional) ─────────────────────────────────
    trail = report.get("trail_history") or []
    if trail:
        t3 = _Table(box=_BOX, style=_STYLE, header_style=_HEAD,
                    title="[dim]Trailing Stop History[/]")
        t3.add_column("Time",   style="dim")
        t3.add_column("Type",   style="bold")
        t3.add_column("Reason", style="dim")
        t3.add_column("Old",    justify="right")
        t3.add_column("New",    justify="right")
        t3.add_column("Price",  justify="right")
        for ev in trail:
            old_v  = ev.get("old")
            new_v  = ev.get("new")
            t3.add_row(
                _ts(ev.get("time"))[:19],
                str(ev.get("type") or ""),
                str(ev.get("reason") or "—"),
                f"{old_v:.4f}" if old_v is not None else "—",
                f"{new_v:.4f}" if new_v is not None else "—",
                f"{ev.get('price'):.4f}" if ev.get("price") is not None else "—",
            )
        con.print(t3)

    # ── Diagnosis panel ────────────────────────────────────────────────────
    diag_colour = {
        DIAG_MISSING_LOGGING:        "yellow",
        DIAG_WRONG_EXIT_POLICY:      "red",
        DIAG_TRAIL_NEVER_ARMED:      "red",
        DIAG_GIVEBACK_TOO_LOOSE:     "orange3",
        DIAG_PROTECTION_TOO_LATE:    "orange3",
        DIAG_WEAK_ENTRY:             "yellow",
        DIAG_STRONG_ENTRY_WEAK_EXIT: "cyan",
        DIAG_CLEAN:                  "green",
    }.get(primary, "white")

    all_diag_lines = "\n".join(f"  • {d}" for d in diags) if diags else f"  • {DIAG_CLEAN}"
    panel_body = (
        f"[bold {diag_colour}]Primary: {primary}[/]\n\n"
        f"[dim]All flags:[/]\n{all_diag_lines}"
    )
    con.print(_Panel(panel_body, title="[bold]Diagnosis[/]", border_style=diag_colour))


# ─── formatters ───────────────────────────────────────────────────────────────

def format_text(report: dict) -> str:
    """Render a report as a plain-text box suitable for terminal output."""
    if "error" in report:
        return f"[FORENSIC ERROR] {report['error']}"

    diags     = report.get("_diagnosis") or []
    diag_str  = ", ".join(diags) if diags else DIAG_CLEAN
    trail     = report.get("trail_history") or []

    lines = [
        "╔" + "═" * 68 + "╗",
        f"║  TRADE FORENSIC REPORT — {report.get('trade_id', '?'):<43}║",
        "╠" + "═" * 68 + "╣",
        f"║  Asset:           {report.get('asset', '-'):<50}║",
        f"║  Timeframe:       {report.get('timeframe', '-'):<50}║",
        f"║  Strategy mode:   {report.get('strategy_mode', '-'):<50}║",
        f"║  Entry reason:    {str(report.get('entry_reason') or '-'):<50}║",
        "╠" + "─" * 68 + "╣",
        f"║  Direction:       {report.get('expected_direction', '-'):<50}║",
        f"║  Init exit policy:{str(report.get('initial_exit_policy') or '-'):<50}║",
        "╠" + "─" * 68 + "╣",
        f"║  Initial stop:    {report.get('initial_stop_value') or '-':<50}║",
        f"║  Break-even armed:{str(report.get('break_even_armed')):<50}║",
        f"║  BE arm time:     {report.get('protected_profit_activation_time') or '-':<50}║",
        "╠" + "─" * 68 + "╣",
        f"║  MFE:             {report.get('max_unrealized_profit', 0.0):+.3f}%  @ {report.get('timestamp_of_mfe', '-'):<37}║",
        f"║  MAE:             {report.get('min_unrealized_profit', 0.0):+.3f}%  @ {report.get('timestamp_of_mae', '-'):<37}║",
        "╠" + "─" * 68 + "╣",
        f"║  Profit lock stage: {report.get('profit_lock_stage', 0):<48}║",
        f"║  Was protected:   {str(report.get('was_protected_profit')):<50}║",
        "╠" + "─" * 68 + "╣",
    ]

    if trail:
        lines.append("║  TRAIL  HISTORY" + " " * 53 + "║")
        lines.append(f"║  {'Time':<20} {'Type':<18} {'Reason':<23} {'Old→New':<5}  ║")
        lines.append("║  " + "-" * 66 + "║")
        for ev in trail:
            _time  = _ts(ev.get("time"))[:19]
            _type  = (ev.get("type") or "")[:17]
            _rsn   = (ev.get("reason") or "-")[:22]
            _old   = ev.get("old")
            _new   = ev.get("new")
            _arrow = f"{_old:.4f}→{_new:.4f}" if _old is not None and _new is not None else "-"
            lines.append(f"║  {_time:<20} {_type:<18} {_rsn:<23} {_arrow:<6}║")
        lines.append("╠" + "─" * 68 + "╣")

    lines += [
        f"║  Exit policy:     {str(report.get('exit_policy_name') or '-'):<50}║",
        f"║  Exit reason:     {str(report.get('exit_reason') or '-'):<50}║",
        f"║  Entry time:      {report.get('entry_time', '-'):<50}║",
        f"║  Exit time:       {report.get('exit_time', '-'):<50}║",
        f"║  Realized PnL:    {report.get('realized_pnl_pct', 0.0):+.3f}%  ({report.get('realized_pnl', 0.0):+.4f} units){' ' * 23}║",
        "╠" + "═" * 68 + "╣",
        f"║  DIAGNOSIS: {diag_str:<55}║",
        "╚" + "═" * 68 + "╝",
    ]
    return "\n".join(lines)


def format_markdown(report: dict) -> str:
    """Render a report as GitHub-flavoured Markdown."""
    if "error" in report:
        return f"# Trade Forensic Report\n\n**Error:** {report['error']}"

    diags    = report.get("_diagnosis") or []
    diag_str = "\n".join(f"- `{d}`" for d in diags) if diags else f"- `{DIAG_CLEAN}`"
    trail    = report.get("trail_history") or []

    trail_rows = ""
    if trail:
        trail_rows = "\n## Trailing Stop History\n\n| Time | Type | Reason | Old | New | Price |\n|---|---|---|---|---|---|\n"
        for ev in trail:
            trail_rows += (
                f"| {_ts(ev.get('time'))} | {ev.get('type','') or ''} "
                f"| {ev.get('reason','') or '-'} "
                f"| {ev.get('old') or '-'} | {ev.get('new') or '-'} "
                f"| {ev.get('price') or '-'} |\n"
            )

    mfe = report.get("max_unrealized_profit", 0.0)
    mae = report.get("min_unrealized_profit", 0.0)

    return f"""# Trade Forensic Report

**Trade ID:** `{report.get('trade_id')}`

## Identity
| Field | Value |
|---|---|
| Asset | {report.get('asset')} |
| Timeframe | {report.get('timeframe')} |
| Strategy Mode | {report.get('strategy_mode')} |
| Entry Reason | {report.get('entry_reason') or '-'} |
| Direction | {report.get('expected_direction')} |
| Initial Exit Policy | {report.get('initial_exit_policy') or '-'} |

## Stops & Protection
| Field | Value |
|---|---|
| Initial Stop Value | {report.get('initial_stop_value') or '-'} |
| Break-even Armed | {report.get('break_even_armed')} |
| BE Activation Time | {report.get('protected_profit_activation_time') or '-'} |
| Profit Lock Stage | {report.get('profit_lock_stage', 0)} |
| Was Protected Profit | {report.get('was_protected_profit')} |

## Excursion (MFE / MAE)
| | Value | Timestamp |
|---|---|---|
| **MFE** (max unrealized profit) | {mfe:+.3f}% | {report.get('timestamp_of_mfe', '-')} |
| **MAE** (max unrealized loss)   | {mae:+.3f}% | {report.get('timestamp_of_mae', '-')} |
{trail_rows}
## Exit
| Field | Value |
|---|---|
| Exit Policy Name | {report.get('exit_policy_name') or '-'} |
| Exit Reason | {report.get('exit_reason') or '-'} |
| Entry Time | {report.get('entry_time', '-')} |
| Exit Time | {report.get('exit_time', '-')} |
| Realized PnL % | {report.get('realized_pnl_pct', 0.0):+.3f}% |
| Realized PnL | {report.get('realized_pnl', 0.0):+.4f} units |

## Diagnosis
{diag_str}
"""


def format_json(report: dict) -> str:
    """Render full report as pretty-printed JSON (all fields, all events)."""
    # Drop the raw _all_events from the top-level to keep it clean but include in nested
    out = {k: v for k, v in report.items() if k != "_all_events"}
    out["lifecycle_events"] = report.get("_all_events", [])
    return json.dumps(out, indent=2, default=str)


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Single-trade forensic report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("trade_id", help="UUID of the trade to inspect")
    parser.add_argument(
        "--format", "-f",
        choices=["text", "md", "json"],
        default="text",
        help="Output format: text (default), md (Markdown), json",
    )
    args = parser.parse_args()

    try:
        from src.data.db import init_db
        init_db()
    except Exception:
        pass   # DB may not exist in all environments; generate_report will handle it

    report = generate_report(args.trade_id)

    if args.format == "json":
        print(format_json(report))
    elif args.format == "md":
        print(format_markdown(report))
    else:
        print(format_text(report))


if __name__ == "__main__":
    main()
