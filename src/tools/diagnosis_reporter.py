"""Diagnosis review reporting tools — Phase 10.

Produces three output formats (terminal Rich, Markdown, JSON) for the
recurring-diagnosis / remediation review workflow.

Key functions
-------------
get_full_review_data(db_path, limit)
    Load trades → aggregate → detect problems → generate suggestions.
    Returns a single data dict consumed by all formatters.

print_diagnosis_review(data, console)
    Rich terminal output: top diagnoses, by-mode, by-asset, problems, suggestions.

diagnosis_review_to_markdown(data)
    Full Markdown report.

diagnosis_review_to_json(data)
    Stable JSON string (4 dp floats).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Imports from Phase 10 modules ────────────────────────────────────────────

from src.tools.diagnosis_aggregator import (
    get_diagnosis_agg_data,
    build_grouped_stats,
    rank_problems,
)
from src.tools.remediation_engine import (
    generate_remediation_suggestions,
    suggestion_to_proposal_input,
    rank_suggestions,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
)


# ── End-to-end data builder ──────────────────────────────────────────────────

def get_full_review_data(
    db_path: Optional[str] = None,
    *,
    limit: int = 10_000,
    min_count: int = 3,
    min_frequency_pct: float = 5.0,
) -> dict:
    """Load all analytical data for Phase 10 reporting.

    Returns
    -------
    Dict containing all aggregated stats, recurring problems, suggestions,
    and escalation candidates.
    """
    data = get_diagnosis_agg_data(
        db_path,
        limit=limit,
        min_count=min_count,
        min_frequency_pct=min_frequency_pct,
    )

    problems    = data["recurring_problems"]
    suggestions = generate_remediation_suggestions(problems)
    ranked_s    = rank_suggestions(suggestions, by="priority")
    escalatable = [s for s in ranked_s if s.is_escalatable]

    data["suggestions"]          = [s.to_dict() for s in ranked_s]
    data["escalatable_count"]    = len(escalatable)
    data["high_priority_count"]  = sum(1 for s in ranked_s if s.escalation_priority == PRIORITY_HIGH)
    data["escalation_candidates"] = [
        suggestion_to_proposal_input(s)
        for s in escalatable
    ]

    return data


# ── Rich terminal output ─────────────────────────────────────────────────────

def print_diagnosis_review(
    data: dict,
    *,
    console: Any = None,
) -> None:
    """Rich terminal output for the Phase 10 review workflow.

    Sections:
    1. Summary panel
    2. Top recurring diagnoses table
    3. By strategy mode table
    4. By asset table (top 10)
    5. By exit reason table
    6. Recurring problems table
    7. Remediation suggestions table
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
        _print_plain(data)
        return

    con   = console or _Console()
    _BOX  = _box.HEAVY_HEAD
    _ST   = "white on grey7"
    _HEAD = "bold bright_cyan on grey15"

    total    = data.get("total_closed", 0)
    n_probs  = len(data.get("recurring_problems") or [])
    n_suggs  = len(data.get("suggestions") or [])
    n_high   = data.get("high_priority_count", 0)
    n_esc    = data.get("escalatable_count", 0)

    # ── 1. Summary panel ──────────────────────────────────────────────────
    con.print(_Panel(
        f"[bold]Closed trades analysed:[/]  {total}\n"
        f"[bold]Recurring problems:[/]      [yellow]{n_probs}[/]\n"
        f"[bold]Suggestions generated:[/]   {n_suggs}  "
        f"([red]{n_high} high-priority[/])\n"
        f"[bold]Escalatable to proposal:[/] [cyan]{n_esc}[/]",
        title="[bold]Owl Stalk — Diagnosis Review[/]",
        border_style="bright_cyan",
    ))

    # ── 2. Top recurring diagnoses ────────────────────────────────────────
    by_diag = data.get("by_primary_diagnosis") or {}
    if by_diag:
        t = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                   title="[dim]By Primary Diagnosis[/]")
        t.add_column("Diagnosis",          style="bold")
        t.add_column("n",                  justify="right")
        t.add_column("Freq %",             justify="right")
        t.add_column("Avg PnL %",          justify="right")
        t.add_column("Avg MFE %",          justify="right")
        t.add_column("Avg Giveback %",     justify="right")
        t.add_column("BE Armed %",         justify="right")
        t.add_column("Protected %",        justify="right")

        for diag, m in sorted(by_diag.items(), key=lambda kv: -kv[1]["count"]):
            pnl_col = _Text(
                f"{m['avg_realized_pnl']:+.2f}",
                style="green" if m["avg_realized_pnl"] > 0 else "red",
            )
            t.add_row(
                diag,
                str(m["count"]),
                f"{m['frequency_pct']:.1f}%",
                pnl_col,
                f"{m['avg_mfe']:+.2f}",
                f"{m['avg_giveback']:.2f}",
                f"{m['break_even_armed_rate']*100:.0f}%",
                f"{m['protected_profit_rate']*100:.0f}%",
            )
        con.print(t)

    # ── 3. By strategy mode ───────────────────────────────────────────────
    by_mode = data.get("by_strategy_mode") or {}
    if by_mode:
        t2 = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                    title="[dim]By Strategy Mode[/]")
        t2.add_column("Mode")
        t2.add_column("n",             justify="right")
        t2.add_column("Avg PnL %",     justify="right")
        t2.add_column("Avg MFE %",     justify="right")
        t2.add_column("Avg Giveback",  justify="right")
        t2.add_column("Top Diagnoses", style="dim")

        for mode, m in sorted(by_mode.items()):
            top = ", ".join(m.get("top_diagnoses") or [])[:60]
            pnl_col = _Text(
                f"{m['avg_realized_pnl']:+.2f}",
                style="green" if m["avg_realized_pnl"] > 0 else "red",
            )
            t2.add_row(mode, str(m["count"]), pnl_col,
                       f"{m['avg_mfe']:+.2f}", f"{m['avg_giveback']:.2f}", top)
        con.print(t2)

    # ── 4. By asset (top 10) ──────────────────────────────────────────────
    by_asset = data.get("by_asset") or {}
    if by_asset:
        top_assets = sorted(by_asset.items(), key=lambda kv: -kv[1]["count"])[:10]
        t3 = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                    title="[dim]By Asset (top 10)[/]")
        t3.add_column("Asset")
        t3.add_column("n",          justify="right")
        t3.add_column("Avg PnL %",  justify="right")
        t3.add_column("Avg MFE %",  justify="right")
        t3.add_column("Top Diag",   style="dim")
        for asset, m in top_assets:
            top = (m.get("top_diagnoses") or ["—"])[0]
            pnl_col = _Text(
                f"{m['avg_realized_pnl']:+.2f}",
                style="green" if m["avg_realized_pnl"] > 0 else "red",
            )
            t3.add_row(asset, str(m["count"]), pnl_col, f"{m['avg_mfe']:+.2f}", top)
        con.print(t3)

    # ── 5. By exit reason ─────────────────────────────────────────────────
    by_exit = data.get("by_exit_reason") or {}
    if by_exit:
        t4 = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                    title="[dim]By Exit Reason[/]")
        t4.add_column("Exit Reason")
        t4.add_column("n",          justify="right")
        t4.add_column("Freq %",     justify="right")
        t4.add_column("Avg PnL %",  justify="right")
        t4.add_column("Avg Giveback", justify="right")
        for reason, m in sorted(by_exit.items(), key=lambda kv: -kv[1]["count"]):
            pnl_col = _Text(
                f"{m['avg_realized_pnl']:+.2f}",
                style="green" if m["avg_realized_pnl"] > 0 else "red",
            )
            t4.add_row(
                reason, str(m["count"]),
                f"{m['frequency_pct']:.1f}%",
                pnl_col,
                f"{m['avg_giveback']:.2f}",
            )
        con.print(t4)

    # ── 6. Recurring problems ─────────────────────────────────────────────
    problems = data.get("problems_by_frequency") or []
    if problems:
        t5 = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                    title="[dim]Recurring Problems (by frequency)[/]")
        t5.add_column("Diagnosis",      style="bold")
        t5.add_column("Dim")
        t5.add_column("Value")
        t5.add_column("n",             justify="right")
        t5.add_column("Freq %",        justify="right")
        t5.add_column("Total PnL Dmg", justify="right", style="red")
        t5.add_column("Avg PnL Dmg",   justify="right", style="red")
        t5.add_column("Mode Conc.")

        for p in problems[:20]:
            t5.add_row(
                p["diagnosis_category"],
                p["group_field"],
                p["group_value"],
                str(p["count"]),
                f"{p['frequency_pct']:.1f}%",
                f"{p['total_pnl_damage']:+.2f}",
                f"{p['avg_pnl_damage']:+.2f}",
                p.get("mode_concentration") or "—",
            )
        con.print(t5)

    # ── 7. Remediation suggestions ────────────────────────────────────────
    suggestions = data.get("suggestions") or []
    if suggestions:
        _PRIORITY_COLOUR = {
            "high": "red", "medium": "yellow", "low": "dim",
        }
        t6 = _Table(box=_BOX, style=_ST, header_style=_HEAD,
                    title="[dim]Remediation Suggestions[/]")
        t6.add_column("Priority",    justify="center")
        t6.add_column("Diagnosis",   style="bold")
        t6.add_column("Action",      style="dim")
        t6.add_column("Mode")
        t6.add_column("Escalatable", justify="center")
        t6.add_column("Evidence",    style="dim")

        for s in suggestions[:20]:
            pri_col = s["escalation_priority"]
            colour  = _PRIORITY_COLOUR.get(pri_col, "white")
            esc     = "[green]YES[/]" if s.get("linked_proposal_type") else "[dim]no[/]"
            t6.add_row(
                f"[{colour}]{pri_col.upper()}[/]",
                s["diagnosis_category"],
                s["suggested_action_type"],
                s.get("strategy_mode") or "—",
                esc,
                (s.get("evidence_summary") or "")[:60],
            )
        con.print(t6)


def _print_plain(data: dict) -> None:
    total   = data.get("total_closed", 0)
    n_probs = len(data.get("recurring_problems") or [])
    n_suggs = len(data.get("suggestions") or [])
    print(f"\n=== Owl Stalk Diagnosis Review ===")
    print(f"Closed trades: {total} | Recurring problems: {n_probs} | Suggestions: {n_suggs}\n")

    by_diag = data.get("by_primary_diagnosis") or {}
    if by_diag:
        print("--- By Primary Diagnosis ---")
        for d, m in sorted(by_diag.items(), key=lambda kv: -kv[1]["count"]):
            print(f"  {d}: n={m['count']} freq={m['frequency_pct']:.1f}% avg_pnl={m['avg_realized_pnl']:+.2f}%")

    suggestions = data.get("suggestions") or []
    if suggestions:
        print("\n--- Remediation Suggestions ---")
        for s in suggestions[:15]:
            esc = " [ESCALATABLE]" if s.get("linked_proposal_type") else ""
            print(f"  [{s['escalation_priority'].upper()}]{esc} {s['diagnosis_category']}: {s['suggested_action_type']}")
            print(f"    {s['reason_summary'][:100]}")


# ── Markdown output ───────────────────────────────────────────────────────────

def diagnosis_review_to_markdown(data: dict) -> str:
    """Full Markdown report for Phase 10 diagnosis review."""
    total   = data.get("total_closed", 0)
    n_probs = len(data.get("recurring_problems") or [])
    n_suggs = len(data.get("suggestions") or [])
    n_high  = data.get("high_priority_count", 0)
    n_esc   = data.get("escalatable_count", 0)

    lines = [
        "# Owl Stalk — Diagnosis Review Report",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Closed trades analysed | {total} |",
        f"| Recurring problems detected | {n_probs} |",
        f"| Remediation suggestions | {n_suggs} |",
        f"| High-priority suggestions | {n_high} |",
        f"| Escalatable to proposals | {n_esc} |",
        "",
    ]

    # By primary diagnosis
    by_diag = data.get("by_primary_diagnosis") or {}
    if by_diag:
        lines += [
            "## Diagnoses by Primary Category",
            "",
            "| Diagnosis | n | Freq % | Avg PnL % | Avg MFE % | Avg Giveback | BE Armed % | Protected % |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for diag, m in sorted(by_diag.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(
                f"| {diag} | {m['count']} | {m['frequency_pct']:.1f}% "
                f"| {m['avg_realized_pnl']:+.2f}% | {m['avg_mfe']:+.2f}% "
                f"| {m['avg_giveback']:.2f}% "
                f"| {m['break_even_armed_rate']*100:.0f}% "
                f"| {m['protected_profit_rate']*100:.0f}% |"
            )
        lines.append("")

    # By strategy mode
    by_mode = data.get("by_strategy_mode") or {}
    if by_mode:
        lines += [
            "## Diagnoses by Strategy Mode",
            "",
            "| Mode | n | Avg PnL % | Avg MFE % | Avg Giveback | Top Diagnoses |",
            "|---|---|---|---|---|---|",
        ]
        for mode, m in sorted(by_mode.items()):
            top = ", ".join(m.get("top_diagnoses") or [])
            lines.append(
                f"| {mode} | {m['count']} | {m['avg_realized_pnl']:+.2f}% "
                f"| {m['avg_mfe']:+.2f}% | {m['avg_giveback']:.2f}% | {top} |"
            )
        lines.append("")

    # By asset (top 10)
    by_asset = data.get("by_asset") or {}
    if by_asset:
        lines += [
            "## Diagnoses by Asset (top 10)",
            "",
            "| Asset | n | Avg PnL % | Avg MFE % | Top Diagnosis |",
            "|---|---|---|---|---|",
        ]
        top_assets = sorted(by_asset.items(), key=lambda kv: -kv[1]["count"])[:10]
        for asset, m in top_assets:
            top = (m.get("top_diagnoses") or ["—"])[0]
            lines.append(
                f"| {asset} | {m['count']} | {m['avg_realized_pnl']:+.2f}% "
                f"| {m['avg_mfe']:+.2f}% | {top} |"
            )
        lines.append("")

    # By exit reason
    by_exit = data.get("by_exit_reason") or {}
    if by_exit:
        lines += [
            "## Diagnoses by Exit Reason",
            "",
            "| Exit Reason | n | Freq % | Avg PnL % | Avg Giveback |",
            "|---|---|---|---|---|",
        ]
        for reason, m in sorted(by_exit.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(
                f"| {reason} | {m['count']} | {m['frequency_pct']:.1f}% "
                f"| {m['avg_realized_pnl']:+.2f}% | {m['avg_giveback']:.2f}% |"
            )
        lines.append("")

    # Recurring problems
    problems = data.get("problems_by_frequency") or []
    if problems:
        lines += [
            "## Recurring Problems",
            "",
            "| Diagnosis | Dimension | Value | n | Freq % | Total PnL Damage | Avg PnL Damage | Mode Conc. |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for p in problems[:20]:
            lines.append(
                f"| {p['diagnosis_category']} | {p['group_field']} | {p['group_value']} "
                f"| {p['count']} | {p['frequency_pct']:.1f}% "
                f"| {p['total_pnl_damage']:+.2f}% | {p['avg_pnl_damage']:+.2f}% "
                f"| {p.get('mode_concentration') or '—'} |"
            )
        lines.append("")

    # Remediation suggestions
    suggestions = data.get("suggestions") or []
    if suggestions:
        lines += [
            "## Remediation Suggestions",
            "",
            "| Priority | Diagnosis | Action | Mode | Escalatable | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        for s in suggestions[:20]:
            esc = "YES" if s.get("linked_proposal_type") else "no"
            lines.append(
                f"| {s['escalation_priority'].upper()} | {s['diagnosis_category']} "
                f"| {s['suggested_action_type']} | {s.get('strategy_mode') or '—'} "
                f"| {esc} | {(s.get('evidence_summary') or '')[:60]} |"
            )
        lines.append("")

    # Escalation-ready suggestions
    escalatable = [s for s in suggestions if s.get("linked_proposal_type")]
    if escalatable:
        lines += [
            "## Suggestions Worth Escalating to Proposals",
            "",
        ]
        for s in escalatable[:10]:
            lines += [
                f"### {s['diagnosis_category']} → `{s['linked_proposal_type']}`",
                "",
                f"**Priority:** {s['escalation_priority'].upper()}  ",
                f"**Action:** {s['suggested_action_type']}  ",
                f"**Mode:** {s.get('strategy_mode') or 'cross-mode'}  ",
                f"**Evidence:** {s.get('evidence_summary') or '—'}  ",
                "",
                f"> {s['reason_summary']}",
                "",
                f"**Impact:** {s['impact_summary']}",
                "",
            ]

    return "\n".join(lines)


# ── JSON output ───────────────────────────────────────────────────────────────

def _round_floats(obj: Any, dp: int = 4) -> Any:
    if isinstance(obj, float):
        return round(obj, dp)
    if isinstance(obj, dict):
        return {k: _round_floats(v, dp) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, dp) for v in obj]
    return obj


def diagnosis_review_to_json(data: dict) -> str:
    """Stable JSON string (4 dp floats) for Phase 10 review data."""
    out = {
        "total_closed":           data.get("total_closed", 0),
        "by_primary_diagnosis":   data.get("by_primary_diagnosis", {}),
        "by_strategy_mode":       data.get("by_strategy_mode", {}),
        "by_asset":               data.get("by_asset", {}),
        "by_exit_reason":         data.get("by_exit_reason", {}),
        "by_timeframe":           data.get("by_timeframe", {}),
        "by_entry_reason_code":   data.get("by_entry_reason_code", {}),
        "recurring_problems":     data.get("recurring_problems", []),
        "problems_by_frequency":  data.get("problems_by_frequency", []),
        "problems_by_pnl_damage": data.get("problems_by_pnl_damage", []),
        "suggestions":            data.get("suggestions", []),
        "high_priority_count":    data.get("high_priority_count", 0),
        "escalatable_count":      data.get("escalatable_count", 0),
        "escalation_candidates":  data.get("escalation_candidates", []),
    }
    # Strip per-trade aggregation from JSON export (too verbose, not needed)
    return json.dumps(_round_floats(out), indent=2, default=str)
