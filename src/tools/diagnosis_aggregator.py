"""Diagnosis aggregation and recurring-problem analytics — Phase 10.

Aggregates forensic diagnoses across all closed trades in the database,
groups them by various dimensions, and detects recurring operational problems.

All operations are **read-only** — no config changes, no proposals created,
no automatic promotions.

Key functions
-------------
aggregate_trade_diagnoses(trades)
    Run diagnose() over every closed trade dict and return per-trade records.

group_by(agg, field)
    Group aggregated records by a chosen dimension.

compute_group_metrics(items, total_count)
    Compute performance and lifecycle statistics for a group of records.

build_grouped_stats(agg, field)
    Convenience: group_by + compute_group_metrics for every group.

detect_recurring_problems(agg, min_count, min_frequency_pct)
    Identify diagnosis/dimension combos that recur above threshold.

rank_problems(problems, by)
    Sort problems by frequency, total_pnl_damage, avg_pnl_damage, etc.

get_diagnosis_agg_data(db_path, limit)
    End-to-end: load trades → aggregate → group → return full data dict.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Import forensic diagnosis engine ─────────────────────────────────────────

from src.tools.forensic_report import (
    diagnose,
    primary_diagnosis,
    DIAG_MISSING_LOGGING,
    DIAG_WRONG_EXIT_POLICY,
    DIAG_TRAIL_NEVER_ARMED,
    DIAG_GIVEBACK_TOO_LOOSE,
    DIAG_PROTECTION_TOO_LATE,
    DIAG_WEAK_ENTRY,
    DIAG_STRONG_ENTRY_WEAK_EXIT,
    DIAG_CLEAN,
)

# ── Constants ─────────────────────────────────────────────────────────────────

# All groupable dimensions
VALID_GROUP_FIELDS = frozenset({
    "primary_diagnosis",
    "strategy_mode",
    "asset",
    "asset_class",
    "timeframe",
    "exit_reason",
    "entry_reason_code",
    # Phase 13: regime dimensions
    "regime_label_at_entry",
    "regime_label_at_exit",
    "macro_regime",
})

# Minimum trades before a group qualifies for recurring-problem detection
_DEFAULT_MIN_COUNT           = 3
_DEFAULT_MIN_FREQUENCY_PCT   = 5.0   # 5 % of all aggregated trades


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        s = val.strip().replace("Z", "+00:00")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    return None


def _duration_secs(entry_val: Any, exit_val: Any) -> Optional[float]:
    entry = _parse_dt(entry_val)
    exit_ = _parse_dt(exit_val)
    if entry is None or exit_ is None:
        return None
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    if exit_.tzinfo is None:
        exit_ = exit_.replace(tzinfo=timezone.utc)
    delta = (exit_ - entry).total_seconds()
    return abs(delta)


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mode_of(items: list[str]) -> Optional[str]:
    """Return the most-frequent non-None string, or None if empty."""
    filtered = [x for x in items if x]
    if not filtered:
        return None
    cnt = Counter(filtered)
    return cnt.most_common(1)[0][0]


def _resolve_macro_regime(regime_label: Optional[str]) -> str:
    """Map a detailed RegimeLabel string to its dominant MacroRegime facet.

    Returns the first facet alphabetically for determinism.  Falls back to
    ``"UNCERTAIN"`` for None / unknown labels.
    """
    if not regime_label or regime_label == "UNKNOWN":
        return "UNCERTAIN"
    try:
        from src.signals.regime_types import _LABEL_TO_MACRO
        facets = _LABEL_TO_MACRO.get(regime_label)
        if facets:
            return sorted(f.value for f in facets)[0]
    except ImportError:
        pass
    return "UNCERTAIN"


# ── Per-trade aggregation ─────────────────────────────────────────────────────

def _empty_trade_events(trade: dict) -> list[dict]:
    """Return an empty events list — used when lifecycle events are unavailable.

    The diagnose() function can still fire on trade-level fields alone,
    so we pass an empty list and let it degrade gracefully.
    """
    return []


def _classify_asset_class(trade: dict) -> str:
    """Infer asset_class from the asset name if not stored on the trade row."""
    # If a future schema addition stores asset_class on trades, use it.
    asset_class = (trade.get("asset_class") or "").strip()
    if asset_class:
        return asset_class
    asset = (trade.get("asset") or "").upper()
    _FOREX_PREFIXES = ("EUR", "GBP", "USD", "JPY", "AUD", "CAD", "CHF", "NZD")
    _EQUITY_SUFFIXES = (".US", ".NYSE", ".NASDAQ", ".LSE")
    if any(asset.endswith(s) for s in _EQUITY_SUFFIXES):
        return "equities"
    if any(asset.startswith(p) for p in _FOREX_PREFIXES) and "/" in asset:
        return "forex"
    return "crypto"


def aggregate_trade_diagnoses(
    trades: list[dict],
    *,
    lifecycle_events_by_id: Optional[dict[str, list[dict]]] = None,
) -> list[dict]:
    """Run diagnose() for every trade and return per-trade aggregation records.

    Parameters
    ----------
    trades:
        List of trade row dicts from ``get_closed_trades()``.
    lifecycle_events_by_id:
        Optional pre-fetched {trade_id: [events]} mapping.  When omitted,
        an empty event list is passed to diagnose() — this means trail/
        timing diagnoses that require event data may not fire, but all
        trade-level diagnoses will (weak entry, giveback, wrong policy, etc.)

    Returns
    -------
    List of per-trade aggregation dicts — one entry per input trade.
    """
    if lifecycle_events_by_id is None:
        lifecycle_events_by_id = {}

    result: list[dict] = []
    for t in trades:
        tid        = t.get("trade_id") or ""
        events     = lifecycle_events_by_id.get(tid, _empty_trade_events(t))
        mfe        = _float(t.get("max_unrealized_profit"))
        mae        = _float(t.get("min_unrealized_profit"))
        pnl        = _float(t.get("pnl_pct"))
        giveback   = max(0.0, mfe - pnl)

        all_diags  = diagnose(t, events)
        primary    = primary_diagnosis(all_diags)

        result.append({
            "trade_id":             tid,
            "asset":                t.get("asset") or "UNKNOWN",
            "timeframe":            t.get("timeframe") or "UNKNOWN",
            "strategy_mode":        t.get("strategy_mode") or "UNKNOWN",
            "asset_class":          _classify_asset_class(t),
            "exit_reason":          t.get("close_reason") or "UNKNOWN",
            "entry_reason_code":    t.get("entry_reason_code") or "UNKNOWN",
            # Phase 13: regime fields for regime-aware grouping
            "regime_label_at_entry": t.get("regime_label_at_entry") or "UNKNOWN",
            "regime_label_at_exit":  t.get("regime_label_at_exit") or "UNKNOWN",
            "macro_regime":          _resolve_macro_regime(t.get("regime_label_at_entry")),
            "regime_changed":        bool(t.get("regime_changed_during_trade")),
            "regime_score_adj":      _float(t.get("regime_score_adjustment")),
            "realized_pnl_pct":     pnl,
            "max_unrealized_profit": mfe,
            "min_unrealized_profit": mae,
            "giveback":             giveback,
            "duration_secs":        _duration_secs(t.get("entry_time"), t.get("exit_time")),
            "was_protected_profit": bool(t.get("was_protected_profit")),
            "break_even_armed":     bool(t.get("break_even_armed")),
            "profit_lock_stage":    int(t.get("profit_lock_stage") or 0),
            "all_diagnoses":        all_diags,
            "primary_diagnosis":    primary,
        })
    return result


# ── Grouping and metrics ──────────────────────────────────────────────────────

def group_by(
    agg: list[dict],
    field: str,
) -> dict[str, list[dict]]:
    """Partition aggregated records by a single dimension.

    Unknown values are mapped to the string ``"UNKNOWN"``.

    Returns
    -------
    dict mapping group_value → list of per-trade dicts.
    """
    if field not in VALID_GROUP_FIELDS:
        raise ValueError(
            f"Invalid group field {field!r}. Valid: {sorted(VALID_GROUP_FIELDS)}"
        )
    groups: dict[str, list[dict]] = {}
    for rec in agg:
        key = str(rec.get(field) or "UNKNOWN")
        groups.setdefault(key, []).append(rec)
    return groups


def compute_group_metrics(
    items: list[dict],
    total_count: int,
) -> dict:
    """Compute per-group statistics from a list of aggregated trade records.

    Parameters
    ----------
    items:
        The subset of per-trade records belonging to this group.
    total_count:
        The total number of aggregated records across all groups (used for
        frequency_pct calculation).

    Returns
    -------
    Metrics dict with standardised keys.
    """
    n = len(items)
    freq_pct = (n / total_count * 100.0) if total_count > 0 else 0.0

    pnls      = [r["realized_pnl_pct"]      for r in items]
    mfes      = [r["max_unrealized_profit"]  for r in items]
    maes      = [r["min_unrealized_profit"]  for r in items]
    givebacks = [r["giveback"]               for r in items]
    durations = [r["duration_secs"] for r in items if r["duration_secs"] is not None]

    protected     = sum(1 for r in items if r["was_protected_profit"])
    be_armed      = sum(1 for r in items if r["break_even_armed"])

    stage_counts = Counter(r["profit_lock_stage"] for r in items)
    stage_rates  = {s: stage_counts.get(s, 0) / n for s in range(4)} if n > 0 else {s: 0.0 for s in range(4)}

    # Top diagnoses by count
    all_d: list[str] = []
    for r in items:
        all_d.extend(r["all_diagnoses"])
    top_diags = [label for label, _ in Counter(all_d).most_common(5)]

    return {
        "count":                  n,
        "frequency_pct":          round(freq_pct, 2),
        "avg_realized_pnl":       round(_safe_mean(pnls), 4),
        "avg_mfe":                round(_safe_mean(mfes), 4),
        "avg_mae":                round(_safe_mean(maes), 4),
        "avg_giveback":           round(_safe_mean(givebacks), 4),
        "avg_duration_secs":      round(_safe_mean(durations), 1) if durations else None,
        "protected_profit_rate":  round(protected / n, 4) if n > 0 else 0.0,
        "break_even_armed_rate":  round(be_armed / n, 4) if n > 0 else 0.0,
        "stage_reach_rates":      {str(k): round(v, 4) for k, v in stage_rates.items()},
        "top_diagnoses":          top_diags,
    }


def build_grouped_stats(
    agg: list[dict],
    field: str,
) -> dict[str, dict]:
    """Return {group_value: metrics_dict} for every distinct value of *field*.

    Convenience wrapper around :func:`group_by` + :func:`compute_group_metrics`.
    """
    groups = group_by(agg, field)
    total  = len(agg)
    return {
        gv: compute_group_metrics(items, total)
        for gv, items in groups.items()
    }


# ── Recurring-problem detection ───────────────────────────────────────────────

def detect_recurring_problems(
    agg: list[dict],
    *,
    min_count: int = _DEFAULT_MIN_COUNT,
    min_frequency_pct: float = _DEFAULT_MIN_FREQUENCY_PCT,
) -> list[dict]:
    """Identify (diagnosis, dimension) combinations that recur above threshold.

    For each primary diagnosis, scan across ``strategy_mode``, ``asset``,
    ``timeframe``, and ``exit_reason`` to find concentrations.

    Parameters
    ----------
    agg:
        Output of :func:`aggregate_trade_diagnoses`.
    min_count:
        Minimum trades with that diagnosis in that group to qualify.
    min_frequency_pct:
        Minimum share of *all* aggregated trades to qualify (%).

    Returns
    -------
    List of recurring-problem dicts, unsorted.
    """
    total = len(agg)
    if total == 0:
        return []

    problems: list[dict] = []

    # For each diagnosis category, examine concentration across dimensions
    _SCAN_DIMS = ("strategy_mode", "asset", "timeframe", "exit_reason")

    # Group by primary_diagnosis first
    by_diag = group_by(agg, "primary_diagnosis")

    for diag_label, diag_trades in by_diag.items():
        if diag_label == DIAG_CLEAN:
            continue

        # Overall recurrence check (diagnosis alone)
        diag_count   = len(diag_trades)
        diag_freq    = diag_count / total * 100.0
        if diag_count >= min_count and diag_freq >= min_frequency_pct:
            problems.append(_make_problem(
                diagnosis_category  = diag_label,
                group_field         = "primary_diagnosis",
                group_value         = diag_label,
                trades_in_group     = diag_trades,
                total_count         = total,
            ))

        # Sub-group concentrations (e.g. SCALP + weak entry)
        for dim in _SCAN_DIMS:
            sub_groups = {}
            for rec in diag_trades:
                k = str(rec.get(dim) or "UNKNOWN")
                sub_groups.setdefault(k, []).append(rec)
            for gv, items in sub_groups.items():
                count = len(items)
                freq  = count / total * 100.0
                if count >= min_count and freq >= min_frequency_pct:
                    problems.append(_make_problem(
                        diagnosis_category  = diag_label,
                        group_field         = dim,
                        group_value         = gv,
                        trades_in_group     = items,
                        total_count         = total,
                    ))

    # De-duplicate by (diagnosis_category, group_field, group_value)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for p in problems:
        key = (p["diagnosis_category"], p["group_field"], p["group_value"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _make_problem(
    *,
    diagnosis_category: str,
    group_field: str,
    group_value: str,
    trades_in_group: list[dict],
    total_count: int,
) -> dict:
    """Build a recurring-problem dict from trades in a (diag × dim) bucket."""
    count    = len(trades_in_group)
    freq     = count / total_count * 100.0 if total_count > 0 else 0.0
    pnls     = [r["realized_pnl_pct"] for r in trades_in_group]
    damages  = [p for p in pnls if p < 0]
    total_dmg = sum(damages)
    avg_dmg   = _safe_mean(damages) if damages else 0.0

    modes   = [r.get("strategy_mode") or "" for r in trades_in_group]
    assets  = [r.get("asset") or "" for r in trades_in_group]
    # Phase 13: regime concentration for regime-aware remediation
    regimes = [r.get("macro_regime") or r.get("regime_label_at_entry") or "" for r in trades_in_group]
    regime_labels = [r.get("regime_label_at_entry") or "" for r in trades_in_group]

    return {
        "problem_id":          str(uuid.uuid4()),
        "diagnosis_category":  diagnosis_category,
        "group_field":         group_field,
        "group_value":         group_value,
        "count":               count,
        "frequency_pct":       round(freq, 2),
        "total_pnl_damage":    round(total_dmg, 4),
        "avg_pnl_damage":      round(avg_dmg, 4),
        "mode_concentration":  _mode_of(modes),
        "asset_concentration": _mode_of(assets),
        "regime_concentration": _mode_of(regimes),
        "regime_label_detail":  _mode_of(regime_labels),
        "affected_trade_ids":  [r["trade_id"] for r in trades_in_group],
    }


# ── Ranking ───────────────────────────────────────────────────────────────────

_VALID_RANK_FIELDS = {
    "frequency",
    "total_pnl_damage",
    "avg_pnl_damage",
    "mode_concentration",
    "asset_concentration",
    "count",
}


def rank_problems(
    problems: list[dict],
    by: str = "frequency",
) -> list[dict]:
    """Sort recurring problems in descending order of *by*.

    Parameters
    ----------
    by:
        One of: ``"frequency"``, ``"total_pnl_damage"``, ``"avg_pnl_damage"``,
        ``"count"``.  Concentration fields sort alphabetically (least useful
        but harmless).
    """
    if by not in _VALID_RANK_FIELDS:
        raise ValueError(f"Invalid rank field {by!r}. Valid: {sorted(_VALID_RANK_FIELDS)}")

    def _key(p: dict) -> float:
        if by == "frequency":
            return -p.get("frequency_pct", 0.0)
        if by in ("total_pnl_damage", "avg_pnl_damage"):
            # damage = negative pnl → already stored as negative, so sort ascending
            return p.get(by, 0.0)
        if by == "count":
            return -p.get("count", 0)
        return 0.0

    return sorted(problems, key=_key)


# ── End-to-end entry point ────────────────────────────────────────────────────

def get_diagnosis_agg_data(
    db_path: Optional[str] = None,
    *,
    limit: int = 10_000,
    min_count: int = _DEFAULT_MIN_COUNT,
    min_frequency_pct: float = _DEFAULT_MIN_FREQUENCY_PCT,
) -> dict:
    """Load trades from DB, run full aggregation pipeline, return structured data.

    Parameters
    ----------
    db_path:
        Optional SQLite path override for test isolation.
    limit:
        Max closed trades to load.
    min_count / min_frequency_pct:
        Recurring-problem detection thresholds.

    Returns
    -------
    Dict with keys:
        ``total_closed``, ``aggregated``, ``by_primary_diagnosis``,
        ``by_strategy_mode``, ``by_asset``, ``by_exit_reason``,
        ``by_timeframe``, ``by_entry_reason_code``,
        ``recurring_problems``, ``problems_by_frequency``,
        ``problems_by_pnl_damage``.
    """
    import src.data.db as db_mod

    if db_path is not None:
        _orig = db_mod.SQLITE_PATH
        db_mod.SQLITE_PATH = db_path
        try:
            trades = db_mod.get_closed_trades(limit=limit)
        finally:
            db_mod.SQLITE_PATH = _orig
    else:
        trades = db_mod.get_closed_trades(limit=limit)

    agg = aggregate_trade_diagnoses(trades)
    problems = detect_recurring_problems(
        agg,
        min_count=min_count,
        min_frequency_pct=min_frequency_pct,
    )

    return {
        "total_closed":            len(trades),
        "aggregated":              agg,
        "by_primary_diagnosis":    build_grouped_stats(agg, "primary_diagnosis"),
        "by_strategy_mode":        build_grouped_stats(agg, "strategy_mode"),
        "by_asset":                build_grouped_stats(agg, "asset"),
        "by_exit_reason":          build_grouped_stats(agg, "exit_reason"),
        "by_timeframe":            build_grouped_stats(agg, "timeframe"),
        "by_entry_reason_code":    build_grouped_stats(agg, "entry_reason_code"),
        "recurring_problems":      problems,
        "problems_by_frequency":   rank_problems(problems, by="frequency"),
        "problems_by_pnl_damage":  rank_problems(problems, by="total_pnl_damage"),
    }
