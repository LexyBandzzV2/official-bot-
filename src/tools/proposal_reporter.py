"""Proposal reporter — Phase 7.

Rich-formatted tables and text serialisers for :class:`src.tools.proposal_engine.ProposalRecord`
dicts (as returned by ``get_proposals()`` or the ``to_dict()`` method).

All functions accept either:
* a list of ``ProposalRecord`` objects, or
* a list of plain dicts (as returned from the DB layer).

Usage::

    from src.data.db import get_proposals
    from src.tools.proposal_reporter import print_proposals_table, proposals_to_markdown_summary

    proposals = get_proposals()                   # all proposals from DB
    print_proposals_table(proposals)              # rich terminal table
    md = proposals_to_markdown_summary(proposals) # markdown string for copy-paste
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Union

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_dict(p: Any) -> dict:
    """Accept a ProposalRecord or a plain dict."""
    if isinstance(p, dict):
        return p
    if hasattr(p, "to_dict"):
        return p.to_dict()
    return vars(p)


def _short_id(proposal_id: str | None, length: int = 8) -> str:
    if not proposal_id:
        return "?"
    return str(proposal_id)[:length]


def _fmt_value(val: Any) -> str:
    """Format a current/proposed value for display (JSON decode if string)."""
    if val is None:
        return "—"
    if isinstance(val, str):
        try:
            decoded = json.loads(val)
            if isinstance(decoded, dict):
                return ", ".join(f"{k}={v}" for k, v in decoded.items())
            return str(decoded)
        except (json.JSONDecodeError, ValueError):
            return val
    if isinstance(val, dict):
        return ", ".join(f"{k}={v}" for k, v in val.items())
    return str(val)


_STATUS_STYLE: dict[str, str] = {
    "draft":                     "dim white",
    "backtest_pending":          "yellow",
    "backtest_complete":         "bright_yellow",
    "paper_validation_pending":  "cyan",
    "paper_validation_complete": "bright_cyan",
    "approved":                  "green",
    "rejected":                  "red",
    "promoted":                  "bold green",
    "superseded":                "dim",
}


# ── Core table builder ────────────────────────────────────────────────────────

def print_proposals_table(
    proposals: list[Any],
    *,
    title: str | None = None,
    console=None,
) -> None:
    """Print a single Rich table for *proposals*.

    Parameters
    ----------
    proposals:
        List of ``ProposalRecord`` objects or dicts.
    title:
        Optional table title override.
    console:
        Existing ``rich.console.Console`` instance; a new one is created if not
        provided.
    """
    if not proposals:
        _console(console).print("[dim]No proposals to display.[/dim]")
        return

    try:
        from rich import box as rich_box
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        _print_proposals_plain(proposals, title=title)
        return

    con = console or Console()
    rows = [_to_dict(p) for p in proposals]

    tbl = Table(
        title=title or f"Optimization Proposals ({len(rows)})",
        box=rich_box.HEAVY_HEAD,
        style="white on grey7",
        header_style="bold bright_cyan on grey15",
        show_lines=False,
        expand=False,
    )
    tbl.add_column("ID",            style="dim",         no_wrap=True, width=10)
    tbl.add_column("Type",          style="bold white",  no_wrap=True)
    tbl.add_column("Mode",          style="cyan",        no_wrap=True, width=14)
    tbl.add_column("Asset",         style="bright_white",no_wrap=True, width=10)
    tbl.add_column("Current",       style="yellow",      no_wrap=False, max_width=22)
    tbl.add_column("Proposed",      style="green",       no_wrap=False, max_width=22)
    tbl.add_column("Status",        style="white",       no_wrap=True)
    tbl.add_column("Created",       style="dim",         no_wrap=True)
    tbl.add_column("Evidence",      style="dim white",   no_wrap=False, max_width=40)

    for r in rows:
        status = r.get("approval_status", "?")
        status_style = _STATUS_STYLE.get(status, "white")
        created = str(r.get("created_at", "?"))[:10]   # date only
        tbl.add_row(
            _short_id(r.get("proposal_id")),
            r.get("proposal_type", "?"),
            r.get("strategy_mode") or "—",
            r.get("asset")         or "—",
            _fmt_value(r.get("current_value")),
            _fmt_value(r.get("proposed_value")),
            Text(status, style=status_style),
            created,
            r.get("evidence_summary") or "—",
        )

    con.print(tbl)


def _console(existing=None):
    """Return *existing* console or create a new one (import-safe)."""
    if existing is not None:
        return existing
    try:
        from rich.console import Console
        return Console()
    except ImportError:
        class _Stub:
            def print(self, *a, **kw):
                print(*a)
        return _Stub()


# ── Grouped views ─────────────────────────────────────────────────────────────

def print_proposals_by_status(proposals: list[Any], *, console=None) -> None:
    """Print one table per approval_status group."""
    con = _console(console)
    groups: dict[str, list] = defaultdict(list)
    for p in proposals:
        d = _to_dict(p)
        groups[d.get("approval_status", "unknown")].append(p)

    order = list(_STATUS_STYLE.keys()) + ["unknown"]
    for status in order:
        if status in groups:
            print_proposals_table(
                groups[status],
                title=f"Proposals — status: {status}  ({len(groups[status])})",
                console=con,
            )


def print_proposals_by_mode(proposals: list[Any], *, console=None) -> None:
    """Print one table per strategy_mode group (plus a cross-mode group)."""
    con = _console(console)
    groups: dict[str, list] = defaultdict(list)
    for p in proposals:
        d = _to_dict(p)
        mode = d.get("strategy_mode") or "CROSS_MODE"
        groups[mode].append(p)

    for mode in sorted(groups.keys()):
        print_proposals_table(
            groups[mode],
            title=f"Proposals — mode: {mode}  ({len(groups[mode])})",
            console=con,
        )


# ── Serialisers ───────────────────────────────────────────────────────────────

def proposals_to_json(proposals: list[Any]) -> str:
    """Serialise *proposals* to a pretty-printed JSON string."""
    return json.dumps([_to_dict(p) for p in proposals], indent=2, default=str)


def proposals_to_markdown_summary(proposals: list[Any]) -> str:
    """Return a Markdown-formatted summary of *proposals*.

    Produces a top-level heading, a summary table, and per-type sections —
    useful for copy-pastes into issue trackers or operation logs.
    """
    rows = [_to_dict(p) for p in proposals]
    if not rows:
        return "# Optimization Proposals\n\n_No proposals._\n"

    lines: list[str] = ["# Optimization Proposals\n"]
    lines.append(f"Generated {len(rows)} proposal(s).\n")

    # Status summary
    from collections import Counter
    status_counts = Counter(r.get("approval_status", "?") for r in rows)
    lines.append("## Status Summary\n")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status, cnt in sorted(status_counts.items()):
        lines.append(f"| {status} | {cnt} |")
    lines.append("")

    # Per-type sections
    type_groups: dict[str, list] = defaultdict(list)
    for r in rows:
        type_groups[r.get("proposal_type", "unknown")].append(r)

    for ptype in sorted(type_groups.keys()):
        lines.append(f"## {ptype.replace('_', ' ').title()}\n")
        lines.append("| ID | Mode | Asset | Current | Proposed | Status | Evidence |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in type_groups[ptype]:
            cells = [
                _short_id(r.get("proposal_id")),
                r.get("strategy_mode") or "—",
                r.get("asset")         or "—",
                _fmt_value(r.get("current_value")),
                _fmt_value(r.get("proposed_value")),
                r.get("approval_status", "?"),
                (r.get("evidence_summary") or "—")[:80],
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    return "\n".join(lines)


# ── Plain-text fallback ───────────────────────────────────────────────────────

def _print_proposals_plain(proposals: list[Any], *, title: str | None = None) -> None:
    """Minimal plain-text output when Rich is unavailable."""
    rows = [_to_dict(p) for p in proposals]
    header = title or f"=== Proposals ({len(rows)}) ==="
    print(header)
    for r in rows:
        print(
            f"  [{_short_id(r.get('proposal_id'))}]"
            f" {r.get('proposal_type','?')}"
            f" | {r.get('strategy_mode') or 'cross'}"
            f" | {r.get('approval_status','?')}"
            f" | {r.get('reason_summary','')}"
        )
