"""Optimization proposal engine — Phase 7.

Generates structured *draft* recommendations from the bot's existing analytics
(signal quality, leakage, candle-fade effectiveness) without applying any
changes automatically.

Usage (programmatic)::

    from src.tools.proposal_engine import generate_proposals
    proposals = generate_proposals(db_path="data/algobot.db")
    for p in proposals:
        print(p.reason_summary, p.evidence_summary)
        # Save manually after review:
        from src.data.db import save_proposal
        save_proposal(p.to_dict())

Approval workflow::

    from src.data.db import transition_proposal_status
    # Work the proposal through the approval pipeline:
    transition_proposal_status(pid, "backtest_pending")
    transition_proposal_status(pid, "backtest_complete")
    transition_proposal_status(pid, "paper_validation_pending")
    transition_proposal_status(pid, "paper_validation_complete")
    transition_proposal_status(pid, "approved")
    # Only from "approved" can a proposal be promoted:
    transition_proposal_status(pid, "promoted")

Design guardrails
-----------------
* ``generate_proposals()`` is *read-only* — it never writes to the database.
  The caller decides which proposals to persist with ``save_proposal()``.
* ``promote_proposal()`` sets ``promoted_at`` but does **not** modify any live
  ``config.py`` or ``exit_policies.py``; that manual step is the operator's
  responsibility after promotion.
* The state machine is enforced by ``transition_proposal_status()`` in
  ``src.data.db``; "promoted" is unreachable from any state except "approved".
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────────────

class ProposalType(str, Enum):
    THRESHOLD_CHANGE               = "threshold_change"
    ML_VETO_CHANGE                 = "ml_veto_change"
    AI_VETO_CHANGE                 = "ai_veto_change"
    INDICATOR_COMBO_PENALTY        = "indicator_combo_penalty"
    INDICATOR_COMBO_BONUS          = "indicator_combo_bonus"
    CANDLE_FADE_REQUIREMENT_CHANGE = "candle_fade_requirement_change"
    ASSET_SPECIFIC_THRESHOLD       = "asset_specific_threshold"
    MODE_SPECIFIC_THRESHOLD        = "mode_specific_threshold"
    EXIT_POLICY_TIGHTENING         = "exit_policy_tightening"
    EXIT_POLICY_RELAXATION         = "exit_policy_relaxation"
    # Phase 13: regime-aware proposal types
    REGIME_THRESHOLD_CHANGE        = "regime_threshold_change"
    REGIME_EXIT_POLICY_CHANGE      = "regime_exit_policy_change"
    REGIME_FADE_REQUIREMENT_CHANGE = "regime_fade_requirement_change"
    REGIME_ML_VETO_CHANGE          = "regime_ml_veto_change"
    REGIME_AI_VETO_CHANGE          = "regime_ai_veto_change"


class ProposalStatus(str, Enum):
    DRAFT                     = "draft"
    BACKTEST_PENDING          = "backtest_pending"
    BACKTEST_COMPLETE         = "backtest_complete"
    PAPER_VALIDATION_PENDING  = "paper_validation_pending"
    PAPER_VALIDATION_COMPLETE = "paper_validation_complete"
    APPROVED                  = "approved"
    REJECTED                  = "rejected"
    PROMOTED                  = "promoted"
    SUPERSEDED                = "superseded"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ProposalRecord:
    """Structured optimization recommendation.

    All proposals start as ``approval_status="draft"`` and must be advanced
    manually through the approval pipeline.  ``generate_proposals()`` always
    returns drafts.

    Attributes
    ----------
    proposal_id:
        Auto-generated UUID string.  Set explicitly only when reconstructing
        a proposal from DB rows.
    proposal_type:
        One of the :class:`ProposalType` string values.
    strategy_mode:
        ``"SCALP"`` / ``"INTERMEDIATE"`` / ``"SWING"`` / ``None`` for
        cross-mode proposals.
    asset:
        Ticker symbol when the proposal targets a specific asset, else None.
    asset_class:
        ``"crypto"`` / ``"forex"`` / ``"equities"`` / None.
    current_value:
        The parameter value that currently applies (Python object; serialised
        as JSON in DB).
    proposed_value:
        The suggested replacement value (Python object; serialised as JSON).
    reason_summary:
        Short human-readable explanation of why this proposal was generated
        (≤ 200 chars, no leading verbs required).
    evidence_summary:
        One-line evidence caption for use in tables (e.g. ``"n=42 win_rate
        SCALP 70–74: 41% vs 75–79: 53% (+12pp)"``).
    evidence_metrics:
        Full analytics dict attached as evidence.  Stored as JSON in DB.
    backtest_status / paper_validation_status:
        ``"pending"`` | ``"pass"`` | ``"fail"`` — updated externally.
    approval_status:
        Starts at ``"draft"``; advances through the state machine defined in
        ``src.data.db._PROPOSAL_VALID_TRANSITIONS``.
    promoted_at:
        ISO timestamp set by ``transition_proposal_status(…, "promoted")``.
    superseded_by:
        ``proposal_id`` of the proposal that replaced this one, if any.
    """
    proposal_type:          str
    reason_summary:         str
    proposal_id:            str              = field(default_factory=lambda: str(uuid.uuid4()))
    created_at:             str              = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    strategy_mode:          Optional[str]    = None
    asset:                  Optional[str]    = None
    asset_class:            Optional[str]    = None
    macro_regime:           Optional[str]    = None
    current_value:          Optional[Any]    = None
    proposed_value:         Optional[Any]    = None
    evidence_summary:       Optional[str]    = None
    evidence_metrics:       Optional[dict]   = None
    backtest_status:        str              = "pending"
    paper_validation_status:str             = "pending"
    approval_status:        str              = "draft"
    promoted_at:            Optional[str]    = None
    superseded_by:          Optional[str]    = None

    def to_dict(self) -> dict:
        """Serialise for ``save_proposal()`` — JSON-encodes value fields."""
        return {
            "proposal_id":             self.proposal_id,
            "created_at":              self.created_at,
            "proposal_type":           self.proposal_type,
            "strategy_mode":           self.strategy_mode,
            "asset":                   self.asset,
            "asset_class":             self.asset_class,
            "macro_regime":            self.macro_regime,
            "current_value":           json.dumps(self.current_value)  if self.current_value  is not None else None,
            "proposed_value":          json.dumps(self.proposed_value) if self.proposed_value is not None else None,
            "reason_summary":          self.reason_summary,
            "evidence_summary":        self.evidence_summary,
            "evidence_metrics_json":   json.dumps(self.evidence_metrics) if self.evidence_metrics else None,
            "backtest_status":         self.backtest_status,
            "paper_validation_status": self.paper_validation_status,
            "approval_status":         self.approval_status,
            "promoted_at":             self.promoted_at,
            "superseded_by":           self.superseded_by,
        }


# ── Approval helpers ──────────────────────────────────────────────────────────

def approve_proposal(proposal_id: str) -> None:
    """Advance *proposal_id* from ``paper_validation_complete`` → ``approved``.

    Thin wrapper around :func:`src.data.db.transition_proposal_status`.
    """
    from src.data.db import transition_proposal_status
    transition_proposal_status(proposal_id, "approved")


def promote_proposal(proposal_id: str) -> None:
    """Advance *proposal_id* from ``approved`` → ``promoted``.

    Sets ``promoted_at`` timestamp.  Does **not** modify any live config;
    the operator must manually apply the proposed change after promotion.

    Raises ``ValueError`` if the proposal is not in ``approved`` state.
    """
    from src.data.db import transition_proposal_status
    transition_proposal_status(proposal_id, "promoted")


# ── Internal thresholds ───────────────────────────────────────────────────────

_SCORE_BAND_WIN_RATE_DELTA   = 0.08   # pp diff between adjacent score bands required to emit
_ML_VETO_RATE_HIGH           = 0.60   # veto rate above which to suggest relaxation
_AI_VETO_RATE_HIGH           = 0.60
_COMBO_PENALTY_MAX_ACCEPT    = 0.15   # accept rate below this → penalty proposal
_COMBO_BONUS_MIN_ACCEPT      = 0.75   # accept rate above this → bonus proposal
_COMBO_MIN_COUNT             = 10     # minimum signals in a combo group to qualify
_CAPTURE_RATIO_TIGHTEN       = 0.40   # capture_ratio below this → tightening proposal
_CAPTURE_RATIO_RELAX         = 0.85   # capture_ratio above this (+ low giveback) → relax
_GIVEBACK_RELAX_MAX          = 0.30   # avg_giveback below this when relaxing
_FADE_IMPROVEMENT_THRESHOLD  = 0.05   # capture ratio difference to emit fade proposal


# ── Internal analyzers ────────────────────────────────────────────────────────

def _conn(db_path: Any) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    return c


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _analyze_score_band_outcomes(db_path: Any, signal_type: str) -> list[ProposalRecord]:
    """Propose threshold changes when adjacent score bands show materially different outcomes.

    Joins the signal table to ``trades`` on ``entry_reason_code`` to see which
    accepted signals led to winning trades, then groups by strategy_mode and
    score band (70–74, 75–79, 80–84, 85+).

    A threshold_change proposal is emitted when band N+1 win rate exceeds band N
    by more than ``_SCORE_BAND_WIN_RATE_DELTA`` (8pp default).
    """
    proposals: list[ProposalRecord] = []
    tbl = "buy_signals" if signal_type.upper() == "BUY" else "sell_signals"
    bands = [(70, 75), (75, 80), (80, 85), (85, 9999)]
    band_labels = {(70, 75): "70–74", (75, 80): "75–79", (80, 85): "80–84", (85, 9999): "85+"}

    try:
        conn = _conn(db_path)
        try:
            for mode in ("SCALP", "INTERMEDIATE", "SWING"):
                band_stats: dict[tuple, dict] = {}
                for lo, hi in bands:
                    hi_clause = "AND s.score_total < ?" if hi < 9999 else ""
                    hi_params = (hi,) if hi < 9999 else ()
                    rows = conn.execute(
                        f"""
                        SELECT t.pnl_pct
                        FROM {tbl} s
                        JOIN trades t ON s.entry_reason_code = t.entry_reason_code
                          AND s.asset = t.asset
                        WHERE s.strategy_mode = ?
                          AND s.accepted_signal = 1
                          AND s.score_total >= ?
                          {hi_clause}
                          AND t.status = 'CLOSED'
                        """,
                        (mode, lo) + hi_params,
                    ).fetchall()
                    if rows:
                        pnls = [r["pnl_pct"] for r in rows]
                        wins = sum(1 for p in pnls if p > 0)
                        band_stats[(lo, hi)] = {
                            "n": len(pnls),
                            "win_rate": wins / len(pnls),
                            "avg_pnl": _safe_mean(pnls),
                        }

                # Compare adjacent bands
                sorted_bands = [b for b in bands if b in band_stats]
                for i in range(len(sorted_bands) - 1):
                    lo_band   = sorted_bands[i]
                    hi_band   = sorted_bands[i + 1]
                    lo_stats  = band_stats[lo_band]
                    hi_stats  = band_stats[hi_band]
                    delta     = hi_stats["win_rate"] - lo_stats["win_rate"]
                    if delta > _SCORE_BAND_WIN_RATE_DELTA and lo_stats["n"] >= 5 and hi_stats["n"] >= 5:
                        lo_lbl = band_labels[lo_band]
                        hi_lbl = band_labels[hi_band]
                        proposals.append(ProposalRecord(
                            proposal_type="threshold_change",
                            strategy_mode=mode,
                            reason_summary=(
                                f"{mode} signals scoring {hi_lbl} have "
                                f"{delta*100:.1f}pp higher win rate than {lo_lbl}"
                            ),
                            evidence_summary=(
                                f"n={lo_stats['n']+hi_stats['n']} | "
                                f"{lo_lbl}: {lo_stats['win_rate']*100:.1f}% win | "
                                f"{hi_lbl}: {hi_stats['win_rate']*100:.1f}% win | "
                                f"delta={delta*100:.1f}pp"
                            ),
                            current_value=lo_band[0],
                            proposed_value=hi_band[0],
                            evidence_metrics={
                                "mode": mode, "signal_type": signal_type,
                                "band_low": {
                                    "range": lo_lbl, **lo_stats,
                                },
                                "band_high": {
                                    "range": hi_lbl, **hi_stats,
                                },
                                "win_rate_delta_pp": round(delta * 100, 2),
                            },
                        ))
        finally:
            conn.close()
    except Exception as exc:
        log.warning("_analyze_score_band_outcomes failed: %s", exc)
    return proposals


def _analyze_ml_ai_gate(db_path: Any, signal_type: str) -> list[ProposalRecord]:
    """Propose ML/AI confidence threshold adjustments based on gate veto rates."""
    proposals: list[ProposalRecord] = []
    try:
        from src.signals.signal_analytics import ml_effect_summary
        mle = ml_effect_summary(db_path, signal_type)

        for prefix, ptype in (("ml", "ml_veto_change"), ("ai", "ai_veto_change")):
            veto_rate  = mle.get(f"{prefix}_veto_rate",  0.0)
            boost_rate = mle.get(f"{prefix}_boost_rate", 0.0)
            vetoed     = mle.get(f"{prefix}_vetoed",     0)
            total      = vetoed + mle.get(f"{prefix}_passed", 0) + mle.get(f"{prefix}_boosted", 0)
            if total < 10:
                continue

            threshold_key = "ML_CONFIDENCE_THRESHOLD" if prefix == "ml" else "AI_CONFIDENCE_THRESHOLD"
            try:
                from src.config import ML_CONFIDENCE_THRESHOLD, AI_CONFIDENCE_THRESHOLD
                cur = ML_CONFIDENCE_THRESHOLD if prefix == "ml" else AI_CONFIDENCE_THRESHOLD
            except ImportError:
                cur = 0.65 if prefix == "ml" else 0.60

            if veto_rate > _ML_VETO_RATE_HIGH:
                proposals.append(ProposalRecord(
                    proposal_type=ptype,
                    reason_summary=(
                        f"{prefix.upper()} gate vetoing {veto_rate*100:.1f}% of signals "
                        f"(n={total}) — threshold may be too aggressive"
                    ),
                    evidence_summary=(
                        f"veto_rate={veto_rate*100:.1f}% boost_rate={boost_rate*100:.1f}% "
                        f"n={total}"
                    ),
                    current_value=cur,
                    proposed_value=round(cur - 0.05, 2),
                    evidence_metrics={
                        "prefix": prefix, "veto_rate": veto_rate,
                        "boost_rate": boost_rate, "total_evaluated": total,
                        "current_threshold": cur,
                    },
                ))
    except Exception as exc:
        log.warning("_analyze_ml_ai_gate failed: %s", exc)
    return proposals


def _analyze_indicator_combos(db_path: Any, signal_type: str) -> list[ProposalRecord]:
    """Emit penalty or bonus proposals for consistently under/over-performing combos."""
    proposals: list[ProposalRecord] = []
    try:
        from src.signals.signal_analytics import indicator_combination_summary
        combos = indicator_combination_summary(db_path, signal_type)
        for combo in combos:
            count       = combo.get("count", 0)
            accept_rate = combo.get("accept_rate", 0.0)
            label       = combo.get("label", "?")
            avg_score   = combo.get("avg_score", 0.0)
            if count < _COMBO_MIN_COUNT:
                continue

            if accept_rate < _COMBO_PENALTY_MAX_ACCEPT:
                proposals.append(ProposalRecord(
                    proposal_type="indicator_combo_penalty",
                    reason_summary=(
                        f"Indicator combo '{label}' has only {accept_rate*100:.1f}% "
                        f"accept rate across {count} signals"
                    ),
                    evidence_summary=(
                        f"combo={label} n={count} accept={accept_rate*100:.1f}% "
                        f"avg_score={avg_score:.1f}"
                    ),
                    current_value=None,
                    proposed_value={"penalty_points": -10, "combo": label},
                    evidence_metrics={
                        "combo_label": label, "count": count,
                        "accept_rate": accept_rate, "avg_score": avg_score,
                    },
                ))
            elif accept_rate > _COMBO_BONUS_MIN_ACCEPT:
                proposals.append(ProposalRecord(
                    proposal_type="indicator_combo_bonus",
                    reason_summary=(
                        f"Indicator combo '{label}' has {accept_rate*100:.1f}% "
                        f"accept rate across {count} signals — eligible for bonus scoring"
                    ),
                    evidence_summary=(
                        f"combo={label} n={count} accept={accept_rate*100:.1f}% "
                        f"avg_score={avg_score:.1f}"
                    ),
                    current_value=None,
                    proposed_value={"bonus_points": 5, "combo": label},
                    evidence_metrics={
                        "combo_label": label, "count": count,
                        "accept_rate": accept_rate, "avg_score": avg_score,
                    },
                ))
    except Exception as exc:
        log.warning("_analyze_indicator_combos failed: %s", exc)
    return proposals


def _analyze_leakage_for_proposals(db_path: Any) -> list[ProposalRecord]:
    """Emit exit-policy proposals from per-mode leakage analytics.

    Reads closed trades via ``get_closed_trades()``, wraps each dict as a
    ``SimpleNamespace`` so the existing ``_compute_mode_stats`` helper (which
    uses ``getattr``) can process them without modification.
    """
    proposals: list[ProposalRecord] = []
    try:
        from src.data.db import get_closed_trades
        from src.backtest.leakage_analyzer import _compute_mode_stats

        trade_dicts = get_closed_trades(limit=5000)
        if not trade_dicts:
            return proposals

        # Adapt dict-list to attribute-accessible objects for _compute_mode_stats
        trade_objs = [SimpleNamespace(**d) for d in trade_dicts]

        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            mode_trades = [
                t for t in trade_objs
                if getattr(t, "strategy_mode", "UNKNOWN") == mode
            ]
            if len(mode_trades) < 5:
                continue

            stats = _compute_mode_stats(mode_trades)
            cap   = stats.get("avg_capture_ratio", 0.0)
            give  = stats.get("avg_giveback",      0.0)
            n     = stats.get("count",             0)

            if cap < _CAPTURE_RATIO_TIGHTEN:
                proposals.append(ProposalRecord(
                    proposal_type="exit_policy_tightening",
                    strategy_mode=mode,
                    reason_summary=(
                        f"{mode} average capture ratio {cap:.2f} — "
                        f"avg {(1-cap)*100:.0f}% of MFE lost before exit"
                    ),
                    evidence_summary=(
                        f"n={n} mode={mode} capture_ratio={cap:.2f} "
                        f"avg_mfe={stats.get('avg_mfe',0.0):+.2f}% "
                        f"avg_pnl={stats.get('avg_realized_pnl',0.0):+.2f}% "
                        f"giveback={give:+.2f}%"
                    ),
                    current_value={"avg_capture_ratio": round(cap, 3)},
                    proposed_value={"action": "tighten_giveback_frac", "direction": "decrease"},
                    evidence_metrics={**stats, "mode": mode},
                ))

            elif cap > _CAPTURE_RATIO_RELAX and give < _GIVEBACK_RELAX_MAX:
                proposals.append(ProposalRecord(
                    proposal_type="exit_policy_relaxation",
                    strategy_mode=mode,
                    reason_summary=(
                        f"{mode} capture ratio {cap:.2f} with low giveback {give:+.2f}% — "
                        f"exit policy may be overly tight"
                    ),
                    evidence_summary=(
                        f"n={n} mode={mode} capture_ratio={cap:.2f} "
                        f"avg_giveback={give:+.2f}%"
                    ),
                    current_value={"avg_capture_ratio": round(cap, 3)},
                    proposed_value={"action": "relax_giveback_frac", "direction": "increase"},
                    evidence_metrics={**stats, "mode": mode},
                ))
    except Exception as exc:
        log.warning("_analyze_leakage_for_proposals failed: %s", exc)
    return proposals


def _analyze_candle_fade_effectiveness(db_path: Any) -> list[ProposalRecord]:
    """Compare outcomes of trades where candle-trail tightening fired vs did not.

    A ``candle_fade_requirement_change`` proposal is emitted when there is a
    material difference in capture ratio between the two groups.
    """
    proposals: list[ProposalRecord] = []
    try:
        conn = _conn(db_path)
        try:
            for mode in ("SCALP", "INTERMEDIATE", "SWING"):
                rows = conn.execute(
                    """
                    SELECT fade_tighten_count, pnl_pct, max_unrealized_profit
                    FROM trades
                    WHERE status = 'CLOSED'
                      AND strategy_mode = ?
                      AND max_unrealized_profit > 0
                    """,
                    (mode,),
                ).fetchall()

                fade_group   = [r for r in rows if (r["fade_tighten_count"] or 0) > 0]
                no_fade_group = [r for r in rows if (r["fade_tighten_count"] or 0) == 0]

                if len(fade_group) < 5 or len(no_fade_group) < 5:
                    continue

                def _cap(group):
                    return _safe_mean([
                        r["pnl_pct"] / r["max_unrealized_profit"]
                        for r in group
                        if (r["max_unrealized_profit"] or 0.0) > 0
                    ])

                fade_cap    = _cap(fade_group)
                no_fade_cap = _cap(no_fade_group)
                delta       = fade_cap - no_fade_cap

                if abs(delta) >= _FADE_IMPROVEMENT_THRESHOLD:
                    direction = "improving" if delta > 0 else "degrading"
                    proposals.append(ProposalRecord(
                        proposal_type="candle_fade_requirement_change",
                        strategy_mode=mode,
                        reason_summary=(
                            f"{mode} candle-trail tightening is {direction} capture ratio "
                            f"by {abs(delta):.2f} ({len(fade_group)} fade trades vs "
                            f"{len(no_fade_group)} non-fade)"
                        ),
                        evidence_summary=(
                            f"n_fade={len(fade_group)} cap_fade={fade_cap:.2f} | "
                            f"n_no_fade={len(no_fade_group)} cap_no_fade={no_fade_cap:.2f} | "
                            f"delta={delta:+.2f}"
                        ),
                        current_value={"fade_tighten_active": True},
                        proposed_value={
                            "action": "increase_confirmation_bars" if delta < 0 else "keep_or_extend",
                            "delta_capture_ratio": round(delta, 4),
                        },
                        evidence_metrics={
                            "mode": mode,
                            "fade_count": len(fade_group),
                            "no_fade_count": len(no_fade_group),
                            "fade_capture_ratio": round(fade_cap, 4),
                            "no_fade_capture_ratio": round(no_fade_cap, 4),
                            "delta": round(delta, 4),
                        },
                    ))
        finally:
            conn.close()
    except Exception as exc:
        log.warning("_analyze_candle_fade_effectiveness failed: %s", exc)
    return proposals


# ── Public API ────────────────────────────────────────────────────────────────

def generate_proposals(
    db_path: Any = None,
    signal_type: str = "BUY",
) -> list[ProposalRecord]:
    """Run all internal analyzers and return a deduplicated list of draft proposals.

    This function is **read-only** — it never writes to the database.  All
    returned proposals have ``approval_status="draft"``; the caller must decide
    which to persist with ``save_proposal(p.to_dict())``.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.  Defaults to ``src.config.SQLITE_PATH``.
    signal_type:
        ``"BUY"`` or ``"SELL"`` — which signal table to analyse.

    Returns
    -------
    list of :class:`ProposalRecord` objects ordered by proposal_type then
    strategy_mode.  Empty when there is insufficient data.
    """
    if db_path is None:
        try:
            from src.config import SQLITE_PATH
            db_path = SQLITE_PATH
        except ImportError:
            db_path = "data/algobot.db"

    all_proposals: list[ProposalRecord] = []

    for analyzer, kwargs in [
        (_analyze_score_band_outcomes,   {"signal_type": signal_type}),
        (_analyze_ml_ai_gate,            {"signal_type": signal_type}),
        (_analyze_indicator_combos,      {"signal_type": signal_type}),
        (_analyze_leakage_for_proposals, {}),
        (_analyze_candle_fade_effectiveness, {}),
    ]:
        try:
            results = analyzer(db_path, **kwargs)
            all_proposals.extend(results)
        except Exception as exc:
            log.debug("Analyzer %s failed: %s", analyzer.__name__, exc)

    # Phase 13: regime-aware proposals
    try:
        from src.tools.regime_learning import generate_regime_proposals
        regime_props = generate_regime_proposals(db_path=db_path, limit=10_000)
        all_proposals.extend(regime_props)
    except Exception as exc:
        log.debug("Regime proposal analyzer failed: %s", exc)

    # Deduplicate: keep first occurrence of same (type, mode, asset, regime) key
    seen: set[tuple] = set()
    deduped: list[ProposalRecord] = []
    for p in all_proposals:
        key = (p.proposal_type, p.strategy_mode, p.asset, p.macro_regime)
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    # Sort for deterministic output
    deduped.sort(key=lambda p: (p.proposal_type, p.strategy_mode or "", p.asset or "", p.macro_regime or ""))

    log.info("generate_proposals: %d proposals generated (signal_type=%s)", len(deduped), signal_type)
    return deduped
