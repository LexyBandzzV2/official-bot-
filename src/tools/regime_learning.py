"""Phase 13 — Regime Learning & Proposals.

Provides regime-specific performance analytics, cross-regime aggregation,
regime-aware remediation suggestions, regime-scoped proposal generation,
and regime suitability summaries.

All functions are **read-only** — they never write to the database or
modify configuration.  Proposal generation returns draft ProposalRecords
that the caller must persist explicitly.

Usage::

    from src.tools.regime_learning import (
        compute_regime_performance,
        compute_cross_regime_analytics,
        generate_regime_suggestions,
        generate_regime_proposals,
        compute_regime_suitability,
    )
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_MACRO_REGIMES = ("TRENDING", "RANGING", "HIGH_VOL", "LOW_VOL", "UNCERTAIN")

_ALL_LABELS = (
    "TRENDING_HIGH_VOL",
    "TRENDING_LOW_VOL",
    "CHOPPY_HIGH_VOL",
    "CHOPPY_LOW_VOL",
    "REVERSAL_TRANSITION",
    "NEWS_DRIVEN_UNSTABLE",
    "UNKNOWN",
)

_MODES = ("SCALP", "INTERMEDIATE", "SWING")

_MIN_TRADES = 5  # minimum trades for a bucket to qualify for conclusions


# ── Macro-regime resolution (mirrors Phase 12 mapping) ───────────────────────

def _resolve_macro(regime_label: Optional[str]) -> str:
    """Map detailed regime label → dominant macro facet (alphabetically first)."""
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


def _all_macro_facets(regime_label: Optional[str]) -> list[str]:
    """Return ALL macro facets for a label (a label can have multiple)."""
    if not regime_label or regime_label == "UNKNOWN":
        return ["UNCERTAIN"]
    try:
        from src.signals.regime_types import _LABEL_TO_MACRO
        facets = _LABEL_TO_MACRO.get(regime_label)
        if facets:
            return sorted(f.value for f in facets)
    except ImportError:
        pass
    return ["UNCERTAIN"]


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _safe_mean(vals: list[float]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _win_rate(trades: list[dict]) -> float:
    wins = [t for t in trades if (t.get("pnl_pct") or 0.0) > 0]
    return round(len(wins) / len(trades), 4) if trades else 0.0


def _avg_field(trades: list[dict], key: str) -> float:
    vals = [float(t.get(key) or 0.0) for t in trades]
    return _safe_mean(vals)


def _capture_ratio(trades: list[dict]) -> float:
    vals = []
    for t in trades:
        mfe = float(t.get("max_unrealized_profit") or 0.0)
        pnl = float(t.get("pnl_pct") or 0.0)
        if mfe > 0.01:
            vals.append(min(pnl / mfe, 1.0))
    return _safe_mean(vals)


def _avg_giveback(trades: list[dict]) -> float:
    vals = []
    for t in trades:
        mfe = float(t.get("max_unrealized_profit") or 0.0)
        pnl = float(t.get("pnl_pct") or 0.0)
        if mfe > 0:
            vals.append(mfe - pnl)
    return _safe_mean(vals)


def _protected_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return round(sum(1 for t in trades if t.get("was_protected_profit")) / len(trades), 4)


def _be_armed_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return round(sum(1 for t in trades if t.get("break_even_armed")) / len(trades), 4)


def _stage_reach_rates(trades: list[dict]) -> dict[str, float]:
    n = len(trades)
    if n == 0:
        return {str(s): 0.0 for s in range(4)}
    counts = Counter(int(t.get("profit_lock_stage") or 0) for t in trades)
    return {str(s): round(counts.get(s, 0) / n, 4) for s in range(4)}


def _top_items(items: list[str], n: int = 5) -> list[str]:
    """Most frequent items."""
    return [label for label, _ in Counter(items).most_common(n)]


def _group_by(records: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in records:
        v = str(r.get(key) or "UNKNOWN")
        groups.setdefault(v, []).append(r)
    return groups


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_trades(db_path: Optional[str] = None, limit: int = 10_000) -> list[dict]:
    """Load closed trades from DB.  Reuses regime_reporter pattern."""
    try:
        from src.tools.regime_reporter import _load_trades as _rr_load
        return _rr_load(db_path=db_path, limit=limit)
    except ImportError:
        return []


def _load_signals(db_path: Optional[str] = None, signal_type: str = "BUY",
                  limit: int = 50_000) -> list[dict]:
    """Load signal acceptance/rejection rows."""
    table = "buy_signals" if signal_type.upper() == "BUY" else "sell_signals"
    try:
        import sqlite3
        from pathlib import Path
        if db_path is None:
            try:
                from src.config import SQLITE_PATH
                db_path = SQLITE_PATH
            except ImportError:
                db_path = "data/algobot.db"
        p = Path(db_path)
        if not p.exists():
            return []
        with sqlite3.connect(str(p), detect_types=sqlite3.PARSE_DECLTYPES) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("_load_signals failed: %s", exc)
        return []


# ── Full bucket metrics ──────────────────────────────────────────────────────

def _compute_full_bucket(trades: list[dict]) -> dict:
    """Compute all Phase 13 metrics for a trade bucket."""
    n = len(trades)

    # Diagnosis frequency
    all_diags: list[str] = []
    try:
        from src.tools.diagnosis_aggregator import aggregate_trade_diagnoses
        agg = aggregate_trade_diagnoses(trades)
        for rec in agg:
            if rec.get("primary_diagnosis"):
                all_diags.append(rec["primary_diagnosis"])
    except ImportError:
        pass

    diag_dist: dict[str, int] = dict(Counter(all_diags).most_common(10))

    # Exit reason distribution
    exit_reasons = [str(t.get("close_reason") or "UNKNOWN") for t in trades]
    exit_dist: dict[str, int] = dict(Counter(exit_reasons).most_common(10))

    return {
        "total_trades":          n,
        "win_rate":              _win_rate(trades),
        "avg_pnl":               _avg_field(trades, "pnl_pct"),
        "avg_mfe":               _avg_field(trades, "max_unrealized_profit"),
        "avg_mae":               _avg_field(trades, "min_unrealized_profit"),
        "avg_giveback":          _avg_giveback(trades),
        "avg_capture_ratio":     _capture_ratio(trades),
        "protected_profit_rate": _protected_rate(trades),
        "break_even_armed_rate": _be_armed_rate(trades),
        "stage_reach_rates":     _stage_reach_rates(trades),
        "avg_score":             _avg_field(trades, "score_total"),
        "avg_ml_effect":         _avg_field(trades, "ml_adjustment_points"),
        "avg_ai_effect":         _avg_field(trades, "ai_adjustment_points"),
        "diagnosis_frequency":   diag_dist,
        "exit_reason_dist":      exit_dist,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 13.2  Regime-Performance Analytics
# ═══════════════════════════════════════════════════════════════════════════════

def compute_regime_performance(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> dict:
    """Per-regime performance analytics at both MacroRegime and RegimeLabel levels.

    Returns
    -------
    dict with keys:
        macro_regime_stats:  {macro_regime: bucket_metrics}
        regime_label_stats:  {regime_label: bucket_metrics}
        signal_acceptance:   {macro_regime: {accepted, rejected, total, accept_rate, avg_score}}
    """
    trades = _load_trades(db_path=db_path, limit=limit)

    # — Macro regime buckets (a trade contributes to ALL its facets) —
    macro_buckets: dict[str, list[dict]] = {m: [] for m in _MACRO_REGIMES}
    for t in trades:
        for facet in _all_macro_facets(t.get("regime_label_at_entry")):
            macro_buckets.setdefault(facet, []).append(t)

    macro_stats = {m: _compute_full_bucket(ts) for m, ts in macro_buckets.items()}

    # — Detailed regime label buckets —
    label_groups = _group_by(trades, "regime_label_at_entry")
    label_stats = {lbl: _compute_full_bucket(ts) for lbl, ts in label_groups.items()}

    # — Signal acceptance by macro regime —
    signal_acceptance = _compute_signal_acceptance_by_regime(db_path=db_path)

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "total_trades":       len(trades),
        "macro_regime_stats": macro_stats,
        "regime_label_stats": label_stats,
        "signal_acceptance":  signal_acceptance,
    }


def _compute_signal_acceptance_by_regime(
    db_path: Optional[str] = None,
    signal_type: str = "BUY",
) -> dict[str, dict]:
    """Signal acceptance/rejection rates stratified by macro regime.

    Links signals to trades via (asset, strategy_mode) and derives regime from
    the trade's regime_label_at_entry.  Falls back gracefully when join fails.
    """
    signals = _load_signals(db_path=db_path, signal_type=signal_type)
    if not signals:
        return {m: {"accepted": 0, "rejected": 0, "total": 0,
                     "accept_rate": 0.0, "avg_score": 0.0} for m in _MACRO_REGIMES}

    # Build regime lookup from trade rows
    trades = _load_trades(db_path=db_path)
    regime_by_asset: dict[str, str] = {}
    for t in trades:
        asset = t.get("asset") or ""
        label = t.get("regime_label_at_entry") or "UNKNOWN"
        if asset:
            regime_by_asset[asset] = label  # last seen regime per asset

    result: dict[str, dict] = {m: {"accepted": 0, "rejected": 0, "total": 0,
                                    "scores": []} for m in _MACRO_REGIMES}
    for sig in signals:
        asset = sig.get("asset") or ""
        label = regime_by_asset.get(asset, "UNKNOWN")
        for facet in _all_macro_facets(label):
            bucket = result.setdefault(facet, {"accepted": 0, "rejected": 0,
                                                "total": 0, "scores": []})
            bucket["total"] += 1
            if sig.get("accepted_signal"):
                bucket["accepted"] += 1
            else:
                bucket["rejected"] += 1
            bucket["scores"].append(float(sig.get("score_total") or 0.0))

    # Compute derived values and drop raw scores
    for m in result:
        b = result[m]
        total = b["total"]
        scores = b.pop("scores", [])
        b["accept_rate"] = round(b["accepted"] / total, 4) if total else 0.0
        b["avg_score"] = _safe_mean(scores)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 13.3  Regime × Mode / Asset / Asset-Class Analytics
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cross_regime_analytics(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> dict:
    """Multi-dimensional regime analytics.

    Returns
    -------
    dict with keys:
        mode_x_macro:       {mode: {macro_regime: bucket_metrics}}
        mode_x_label:       {mode: {regime_label: bucket_metrics}}
        asset_x_macro:      {asset: {macro_regime: bucket_metrics}}
        asset_class_x_macro:{asset_class: {macro_regime: bucket_metrics}}
    """
    trades = _load_trades(db_path=db_path, limit=limit)

    mode_x_macro:  dict[str, dict[str, dict]] = {}
    mode_x_label:  dict[str, dict[str, dict]] = {}
    asset_x_macro: dict[str, dict[str, dict]] = {}
    aclass_x_macro: dict[str, dict[str, dict]] = {}

    # Pre-group by first dimension, then sub-group by regime
    by_mode = _group_by(trades, "strategy_mode")
    for mode, mode_trades in by_mode.items():
        # macro sub-groups
        macro_sub: dict[str, list[dict]] = {}
        label_sub: dict[str, list[dict]] = {}
        for t in mode_trades:
            label = str(t.get("regime_label_at_entry") or "UNKNOWN")
            label_sub.setdefault(label, []).append(t)
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)
        mode_x_macro[mode] = {m: _compute_full_bucket(ts) for m, ts in macro_sub.items()}
        mode_x_label[mode] = {l: _compute_full_bucket(ts) for l, ts in label_sub.items()}

    by_asset = _group_by(trades, "asset")
    for asset, asset_trades in by_asset.items():
        macro_sub = {}
        for t in asset_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)
        asset_x_macro[asset] = {m: _compute_full_bucket(ts) for m, ts in macro_sub.items()}

    by_aclass = _group_by(trades, "asset_class")
    for aclass, aclass_trades in by_aclass.items():
        macro_sub = {}
        for t in aclass_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)
        aclass_x_macro[aclass] = {m: _compute_full_bucket(ts) for m, ts in macro_sub.items()}

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "total_trades":       len(trades),
        "mode_x_macro":       mode_x_macro,
        "mode_x_label":       mode_x_label,
        "asset_x_macro":      asset_x_macro,
        "asset_class_x_macro": aclass_x_macro,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 13.4  Regime-Aware Remediation Suggestions
# ═══════════════════════════════════════════════════════════════════════════════

def generate_regime_suggestions(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> list[dict]:
    """Produce advisory-only suggestions tied to regime context.

    Each suggestion is a dict with:
        macro_regime, regime_label, strategy_mode, asset, suggestion_type,
        reason, evidence, severity
    """
    trades = _load_trades(db_path=db_path, limit=limit)
    if len(trades) < _MIN_TRADES:
        return []

    suggestions: list[dict] = []

    # Analyse mode × macro regime performance
    by_mode = _group_by(trades, "strategy_mode")
    for mode, mode_trades in by_mode.items():
        macro_sub: dict[str, list[dict]] = {}
        for t in mode_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)

        for regime, regime_trades in macro_sub.items():
            n = len(regime_trades)
            if n < _MIN_TRADES:
                continue

            wr = _win_rate(regime_trades)
            avg_pnl = _avg_field(regime_trades, "pnl_pct")
            avg_gb = _avg_giveback(regime_trades)
            prot_rate = _protected_rate(regime_trades)

            # Rule 1: Mode underperforms in regime → suggest higher threshold
            if wr < 0.40 and avg_pnl < -0.1:
                suggestions.append({
                    "macro_regime":   regime,
                    "strategy_mode":  mode,
                    "suggestion_type": "threshold_hardening",
                    "reason": (
                        f"{mode} underperforms in {regime}: win_rate={wr:.0%}, "
                        f"avg_pnl={avg_pnl:.2f}%. Consider higher score threshold "
                        f"or disabling low-score entries."
                    ),
                    "evidence": {
                        "trades": n, "win_rate": wr, "avg_pnl": avg_pnl,
                    },
                    "severity": "high" if avg_pnl < -0.5 else "medium",
                })

            # Rule 2: High giveback → suggest earlier protection
            if avg_gb > 1.0 and regime in ("HIGH_VOL", "RANGING"):
                suggestions.append({
                    "macro_regime":   regime,
                    "strategy_mode":  mode,
                    "suggestion_type": "earlier_protection",
                    "reason": (
                        f"{mode} in {regime} shows avg giveback of {avg_gb:.2f}%. "
                        f"Consider earlier break-even arming or tighter exit policy."
                    ),
                    "evidence": {
                        "trades": n, "avg_giveback": avg_gb,
                        "protected_rate": prot_rate,
                    },
                    "severity": "high" if avg_gb > 2.0 else "medium",
                })

            # Rule 3: Trending with good stats → suggest relaxed threshold
            if regime == "TRENDING" and wr > 0.55 and avg_pnl > 0.3:
                suggestions.append({
                    "macro_regime":   regime,
                    "strategy_mode":  mode,
                    "suggestion_type": "threshold_relaxation",
                    "reason": (
                        f"{mode} performs well in {regime}: win_rate={wr:.0%}, "
                        f"avg_pnl={avg_pnl:.2f}%. Consider relaxed threshold or "
                        f"score bonus to capture more entries."
                    ),
                    "evidence": {
                        "trades": n, "win_rate": wr, "avg_pnl": avg_pnl,
                    },
                    "severity": "low",
                })

            # Rule 4: Low protection rate in volatile regime
            if regime == "HIGH_VOL" and prot_rate < 0.3 and n >= _MIN_TRADES:
                suggestions.append({
                    "macro_regime":   regime,
                    "strategy_mode":  mode,
                    "suggestion_type": "protection_improvement",
                    "reason": (
                        f"{mode} in {regime} has low protection rate ({prot_rate:.0%}). "
                        f"Consider smaller position size or faster profit lock."
                    ),
                    "evidence": {
                        "trades": n, "protected_rate": prot_rate,
                        "avg_mae": _avg_field(regime_trades, "min_unrealized_profit"),
                    },
                    "severity": "medium",
                })

    return suggestions


# ═══════════════════════════════════════════════════════════════════════════════
# 13.5  Regime-Aware Proposal Generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_regime_proposals(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> list:
    """Generate draft-only regime-scoped proposals.

    This function is **read-only** and never writes to the database.
    All proposals have ``approval_status="draft"``.

    Returns
    -------
    list of ProposalRecord objects.
    """
    try:
        from src.tools.proposal_engine import ProposalRecord, ProposalType
    except ImportError:
        log.warning("proposal_engine not available; skipping regime proposals")
        return []

    trades = _load_trades(db_path=db_path, limit=limit)
    if len(trades) < _MIN_TRADES:
        return []

    proposals: list = []

    by_mode = _group_by(trades, "strategy_mode")
    for mode, mode_trades in by_mode.items():
        macro_sub: dict[str, list[dict]] = {}
        for t in mode_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)

        for regime, regime_trades in macro_sub.items():
            n = len(regime_trades)
            if n < _MIN_TRADES:
                continue

            wr = _win_rate(regime_trades)
            avg_pnl = _avg_field(regime_trades, "pnl_pct")
            avg_gb = _avg_giveback(regime_trades)
            avg_score = _avg_field(regime_trades, "score_total")
            prot_rate = _protected_rate(regime_trades)

            evidence = {
                "source": "regime_learning",
                "macro_regime": regime,
                "strategy_mode": mode,
                "trades": n,
                "win_rate": wr,
                "avg_pnl": avg_pnl,
                "avg_giveback": avg_gb,
                "avg_score": avg_score,
                "protected_rate": prot_rate,
            }

            # Proposal: threshold change when mode underperforms in regime
            if wr < 0.40 and avg_pnl < -0.1 and n >= _MIN_TRADES:
                proposals.append(ProposalRecord(
                    proposal_type=ProposalType.REGIME_THRESHOLD_CHANGE.value,
                    strategy_mode=mode,
                    macro_regime=regime,
                    current_value={"score_threshold": avg_score},
                    proposed_value={"score_threshold": round(avg_score + 5, 1)},
                    reason_summary=(
                        f"{mode} in {regime}: {wr:.0%} win rate, "
                        f"{avg_pnl:.2f}% avg PnL over {n} trades"
                    )[:200],
                    evidence_summary=f"n={n} win={wr:.0%} pnl={avg_pnl:.2f}%",
                    evidence_metrics=evidence,
                ))

            # Proposal: exit policy tightening for high-giveback regimes
            if avg_gb > 1.0 and regime in ("HIGH_VOL", "RANGING"):
                proposals.append(ProposalRecord(
                    proposal_type=ProposalType.REGIME_EXIT_POLICY_CHANGE.value,
                    strategy_mode=mode,
                    macro_regime=regime,
                    current_value={"avg_giveback": avg_gb},
                    proposed_value={"action": "tighten_exit_policy"},
                    reason_summary=(
                        f"{mode} in {regime}: avg giveback {avg_gb:.2f}%, "
                        f"protection rate {prot_rate:.0%}"
                    )[:200],
                    evidence_summary=f"n={n} giveback={avg_gb:.2f}% prot={prot_rate:.0%}",
                    evidence_metrics=evidence,
                ))

            # Proposal: fade requirement change for ranging
            if regime == "RANGING" and wr < 0.45:
                proposals.append(ProposalRecord(
                    proposal_type=ProposalType.REGIME_FADE_REQUIREMENT_CHANGE.value,
                    strategy_mode=mode,
                    macro_regime=regime,
                    current_value={"win_rate": wr},
                    proposed_value={"action": "require_stronger_fade_confirmation"},
                    reason_summary=(
                        f"{mode} in RANGING: {wr:.0%} win rate suggests "
                        f"candle fade confirmation may need tightening"
                    )[:200],
                    evidence_summary=f"n={n} win={wr:.0%} regime=RANGING",
                    evidence_metrics=evidence,
                ))

            # Proposal: ML/AI veto adjustment
            avg_ml = _avg_field(regime_trades, "ml_adjustment_points")
            avg_ai = _avg_field(regime_trades, "ai_adjustment_points")
            if avg_pnl < -0.2 and (avg_ml < -2.0 or avg_ai < -2.0):
                veto_type = (ProposalType.REGIME_ML_VETO_CHANGE.value
                             if avg_ml < avg_ai
                             else ProposalType.REGIME_AI_VETO_CHANGE.value)
                proposals.append(ProposalRecord(
                    proposal_type=veto_type,
                    strategy_mode=mode,
                    macro_regime=regime,
                    current_value={"avg_ml_effect": avg_ml, "avg_ai_effect": avg_ai},
                    proposed_value={"action": "adjust_veto_sensitivity"},
                    reason_summary=(
                        f"{mode} in {regime}: negative ML/AI effects "
                        f"(ML={avg_ml:.1f}, AI={avg_ai:.1f}) with {avg_pnl:.2f}% avg PnL"
                    )[:200],
                    evidence_summary=f"n={n} ml={avg_ml:.1f} ai={avg_ai:.1f}",
                    evidence_metrics={**evidence, "avg_ml_effect": avg_ml,
                                      "avg_ai_effect": avg_ai},
                ))

    # Deduplicate by (type, mode, regime)
    seen: set[tuple] = set()
    deduped: list = []
    for p in proposals:
        key = (p.proposal_type, p.strategy_mode, p.macro_regime)
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    deduped.sort(key=lambda p: (p.proposal_type, p.strategy_mode or "", p.macro_regime or ""))
    log.info("generate_regime_proposals: %d proposals generated", len(deduped))
    return deduped


# ═══════════════════════════════════════════════════════════════════════════════
# 13.6  Regime Suitability Summaries
# ═══════════════════════════════════════════════════════════════════════════════

def compute_regime_suitability(
    db_path: Optional[str] = None,
    limit: int = 10_000,
) -> dict:
    """Produce a human-readable regime suitability assessment.

    Returns
    -------
    dict with keys:
        mode_suitability:  [{mode, best_regime, worst_regime, summary}]
        asset_suitability: [{asset, best_regime, worst_regime, summary}]
        findings:          [str]  — plain-English bullet points
    """
    trades = _load_trades(db_path=db_path, limit=limit)
    if len(trades) < _MIN_TRADES:
        return {"mode_suitability": [], "asset_suitability": [], "findings": []}

    findings: list[str] = []

    # ── Mode suitability ──
    mode_suit: list[dict] = []
    by_mode = _group_by(trades, "strategy_mode")
    for mode, mode_trades in by_mode.items():
        macro_sub: dict[str, list[dict]] = {}
        for t in mode_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)

        qualified = {
            m: {"n": len(ts), "wr": _win_rate(ts), "pnl": _avg_field(ts, "pnl_pct")}
            for m, ts in macro_sub.items() if len(ts) >= _MIN_TRADES
        }
        if not qualified:
            continue

        best = max(qualified, key=lambda m: qualified[m]["pnl"])
        worst = min(qualified, key=lambda m: qualified[m]["pnl"])

        best_s = qualified[best]
        worst_s = qualified[worst]

        summary = (
            f"{mode} performs best in {best} "
            f"(n={best_s['n']}, {best_s['wr']:.0%} win, {best_s['pnl']:.2f}% pnl) "
            f"and worst in {worst} "
            f"(n={worst_s['n']}, {worst_s['wr']:.0%} win, {worst_s['pnl']:.2f}% pnl)"
        )
        mode_suit.append({
            "mode": mode, "best_regime": best, "worst_regime": worst,
            "best_stats": best_s, "worst_stats": worst_s, "summary": summary,
        })
        findings.append(summary)

        # Flag modes that should be limited in certain regimes
        if worst_s["wr"] < 0.35 and worst_s["pnl"] < -0.3:
            warn = (
                f"{mode} in {worst} is low quality and may deserve filtering "
                f"({worst_s['wr']:.0%} win rate, {worst_s['pnl']:.2f}% avg pnl)"
            )
            findings.append(warn)

    # ── Asset suitability ──
    asset_suit: list[dict] = []
    by_asset = _group_by(trades, "asset")
    for asset, asset_trades in by_asset.items():
        macro_sub = {}
        for t in asset_trades:
            for facet in _all_macro_facets(t.get("regime_label_at_entry")):
                macro_sub.setdefault(facet, []).append(t)

        qualified = {
            m: {"n": len(ts), "wr": _win_rate(ts), "pnl": _avg_field(ts, "pnl_pct")}
            for m, ts in macro_sub.items() if len(ts) >= _MIN_TRADES
        }
        if not qualified:
            continue

        best = max(qualified, key=lambda m: qualified[m]["pnl"])
        worst = min(qualified, key=lambda m: qualified[m]["pnl"])
        best_s = qualified[best]
        worst_s = qualified[worst]

        summary = (
            f"{asset} shines in {best} (n={best_s['n']}, {best_s['pnl']:.2f}%) "
            f"and is noisy in {worst} (n={worst_s['n']}, {worst_s['pnl']:.2f}%)"
        )
        asset_suit.append({
            "asset": asset, "best_regime": best, "worst_regime": worst,
            "best_stats": best_s, "worst_stats": worst_s, "summary": summary,
        })
        findings.append(summary)

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "mode_suitability":  mode_suit,
        "asset_suitability": asset_suit,
        "findings":          findings,
    }
