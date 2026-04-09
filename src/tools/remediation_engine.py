"""Remediation suggestion engine and escalation-to-proposal support — Phase 10.

Converts recurring operational problems (from diagnosis_aggregator) into
structured remediation suggestions, and provides helpers to convert a
suggestion into a draft ProposalRecord input.

All outputs are **recommendations only** — no config changes, no automatic
proposalpromotion.  Human review is mandatory before any escalation proceeds.

Key functions
-------------
generate_remediation_suggestions(problems)
    Produce a RemediationSuggestion for each recurring problem dict.

suggestion_to_proposal_input(suggestion)
    Convert an escalatable suggestion to a ProposalRecord-compatible dict,
    or return None when the problem is operational only (can't become a proposal).

RemediationSuggestion (dataclass)
    Structured representation of one suggestion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from src.tools.forensic_report import (
    DIAG_MISSING_LOGGING,
    DIAG_WRONG_EXIT_POLICY,
    DIAG_TRAIL_NEVER_ARMED,
    DIAG_GIVEBACK_TOO_LOOSE,
    DIAG_PROTECTION_TOO_LATE,
    DIAG_WEAK_ENTRY,
    DIAG_STRONG_ENTRY_WEAK_EXIT,
)
from src.tools.proposal_engine import ProposalType

# ── Action types ──────────────────────────────────────────────────────────────

ACT_TIGHTEN_ENTRY_THRESHOLD = "tighten_entry_threshold"
ACT_TIGHTEN_EXIT_POLICY     = "tighten_exit_policy"
ACT_INSPECT_TRAIL           = "inspect_trail_activation"
ACT_AUDIT_POLICY_ROUTING    = "audit_policy_routing"
ACT_ADD_INSTRUMENTATION     = "add_instrumentation"
ACT_REVIEW_COMBO            = "review_indicator_combo"

# ── Escalation priority ───────────────────────────────────────────────────────

PRIORITY_HIGH   = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW    = "low"

# ── Diagnosis → remediation rules ─────────────────────────────────────────────
# Each entry maps a diagnosis category to:
# (action_type, linked_proposal_type | None, priority, description template)

_RULES: dict[str, tuple[str, Optional[str], str, str, str]] = {
    # diag: (action_type, proposal_type_value|None, priority, reason_tpl, impact_tpl)
    DIAG_WEAK_ENTRY: (
        ACT_TIGHTEN_ENTRY_THRESHOLD,
        ProposalType.THRESHOLD_CHANGE,
        PRIORITY_HIGH,
        "Repeated weak entry detected: fewer than 2 indicators fired or MFE < 0.5%.",
        "Accepting low-quality entries degrades win rate and increases MAE exposure.",
    ),
    DIAG_GIVEBACK_TOO_LOOSE: (
        ACT_TIGHTEN_EXIT_POLICY,
        ProposalType.EXIT_POLICY_TIGHTENING,
        PRIORITY_HIGH,
        "Repeated peak-giveback too loose: PEAK_GIVEBACK_EXIT with capture ratio < 30%.",
        "Significant portion of unrealized profit is surrendered before exit triggers.",
    ),
    DIAG_TRAIL_NEVER_ARMED: (
        ACT_INSPECT_TRAIL,
        None,                  # Operational fix — not a config proposal
        PRIORITY_MEDIUM,
        "Trailing stop never advances beyond initial_stop: trail may not be arming correctly.",
        "Trades rely entirely on initial stop, increasing drawdown and missed profit protection.",
    ),
    DIAG_WRONG_EXIT_POLICY: (
        ACT_AUDIT_POLICY_ROUTING,
        None,                  # Operational fix — review routing/fallback logic
        PRIORITY_HIGH,
        "Exit policy mismatched to strategy mode (e.g. SCALP using ALLIGATOR_TP or swing fallback).",
        "Wrong-mode exit policies misalign holding time expectations and stop distances.",
    ),
    DIAG_PROTECTION_TOO_LATE: (
        ACT_TIGHTEN_EXIT_POLICY,
        ProposalType.EXIT_POLICY_TIGHTENING,
        PRIORITY_MEDIUM,
        "Break-even protection activated in final 20% of trade duration (too late to protect).",
        "Late activation means profit protection fires only after significant drawdown has already occurred.",
    ),
    DIAG_STRONG_ENTRY_WEAK_EXIT: (
        ACT_TIGHTEN_EXIT_POLICY,
        ProposalType.EXIT_POLICY_TIGHTENING,
        PRIORITY_HIGH,
        "Strong entries (good MFE) consistently exit with < 30% capture of maximum excursion.",
        "High-quality entries are not being monetised — major profit leakage pattern.",
    ),
    DIAG_MISSING_LOGGING: (
        ACT_ADD_INSTRUMENTATION,
        None,                  # Instrumentation gap — not a param proposal
        PRIORITY_LOW,
        "Trades lack Phase 3+ lifecycle fields; forensic diagnosis accuracy is reduced.",
        "Without entry_reason, MFE/MAE, and lifecycle events, pattern detection is unreliable.",
    ),
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RemediationSuggestion:
    """Structured remediation recommendation for a recurring diagnosis pattern.

    Attributes
    ----------
    suggestion_id:
        Auto-generated UUID.
    diagnosis_category:
        Exact DIAG_* label string from forensic_report.
    strategy_mode:
        Dominant mode concentration, or None for cross-mode patterns.
    asset:
        Dominant asset concentration, or None.
    asset_class:
        Asset class, or None.
    reason_summary:
        Human-readable description of why this suggestion was generated (≤200 chars).
    evidence_summary:
        One-line metric caption (e.g. "n=12, SCALP, avg_giveback=2.1%").
    impact_summary:
        Explanation of operational impact if the problem is not addressed.
    suggested_action_type:
        One of the ACT_* constants in this module.
    linked_proposal_type:
        ProposalType value if this can become a proposal, else None.
    escalation_priority:
        ``"high"`` | ``"medium"`` | ``"low"``.
    count:
        Number of trades contributing to this pattern.
    frequency_pct:
        Fraction of all analysed trades, as a percentage.
    total_pnl_damage:
        Sum of negative PnL from affected trades.
    avg_pnl_damage:
        Average negative PnL per affected trade.
    source_problem_id:
        problem_id from the originating recurring-problem dict.
    """
    diagnosis_category:   str
    reason_summary:       str
    evidence_summary:     str
    impact_summary:       str
    suggested_action_type: str
    escalation_priority:  str
    count:                int
    frequency_pct:        float
    total_pnl_damage:     float
    avg_pnl_damage:       float
    suggestion_id:        str              = field(default_factory=lambda: str(uuid.uuid4()))
    strategy_mode:        Optional[str]    = None
    asset:                Optional[str]    = None
    asset_class:          Optional[str]    = None
    linked_proposal_type: Optional[str]   = None
    source_problem_id:    Optional[str]   = None
    # Phase 13: regime context
    regime_concentration: Optional[str]    = None
    regime_label_detail:  Optional[str]    = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_escalatable(self) -> bool:
        """True when this suggestion can be converted to a proposal candidate."""
        return self.linked_proposal_type is not None


# ── Suggestion generation ─────────────────────────────────────────────────────

def generate_remediation_suggestions(
    problems: list[dict],
) -> list[RemediationSuggestion]:
    """Convert a list of recurring-problem dicts into RemediationSuggestions.

    One suggestion is generated per problem.  Problems with unknown diagnosis
    categories receive a generic operational suggestion.

    Parameters
    ----------
    problems:
        Output of :func:`diagnosis_aggregator.detect_recurring_problems`.

    Returns
    -------
    List of :class:`RemediationSuggestion` objects, one per problem.
    """
    suggestions: list[RemediationSuggestion] = []

    for prob in problems:
        diag  = prob.get("diagnosis_category", "")
        count = prob.get("count", 0)
        freq  = prob.get("frequency_pct", 0.0)
        total_dmg = prob.get("total_pnl_damage", 0.0)
        avg_dmg   = prob.get("avg_pnl_damage", 0.0)
        mode  = prob.get("mode_concentration")
        asset = prob.get("asset_concentration")
        pid   = prob.get("problem_id")
        gfield = prob.get("group_field", "")
        gvalue = prob.get("group_value", "")
        # Phase 13: extract regime concentration if grouped by regime
        regime_conc = prob.get("regime_concentration")
        regime_detail = prob.get("regime_label_detail")

        rule = _RULES.get(diag)

        if rule is None:
            # Generic suggestion for unrecognised/future diagnosis labels
            suggestions.append(RemediationSuggestion(
                diagnosis_category    = diag,
                reason_summary        = f"Recurring unrecognised diagnosis pattern: {diag!r}.",
                evidence_summary      = _evidence(count, freq, mode, asset, gfield, gvalue, regime_conc),
                impact_summary        = "Pattern frequency warrants operational review.",
                suggested_action_type = "manual_review",
                escalation_priority   = PRIORITY_LOW,
                count                 = count,
                frequency_pct         = freq,
                total_pnl_damage      = total_dmg,
                avg_pnl_damage        = avg_dmg,
                strategy_mode         = mode,
                asset                 = asset,
                linked_proposal_type  = None,
                source_problem_id     = pid,
                regime_concentration  = regime_conc,
                regime_label_detail   = regime_detail,
            ))
            continue

        action, prop_type, priority, reason_tpl, impact_tpl = rule

        # Contextualise the reason with group info
        context_parts = []
        if gfield not in ("primary_diagnosis",) and gvalue not in ("UNKNOWN", diag):
            context_parts.append(f"{gfield}={gvalue!r}")
        if mode and mode != "UNKNOWN":
            context_parts.append(f"mode={mode}")
        if asset and asset != "UNKNOWN" and gfield != "asset":
            context_parts.append(f"asset={asset}")
        ctx = " · ".join(context_parts)
        reason = f"{reason_tpl}{' [' + ctx + ']' if ctx else ''}"
        if len(reason) > 200:
            reason = reason[:197] + "..."

        suggestions.append(RemediationSuggestion(
            diagnosis_category    = diag,
            reason_summary        = reason,
            evidence_summary      = _evidence(count, freq, mode, asset, gfield, gvalue, regime_conc),
            impact_summary        = impact_tpl,
            suggested_action_type = action,
            escalation_priority   = priority,
            count                 = count,
            frequency_pct         = freq,
            total_pnl_damage      = total_dmg,
            avg_pnl_damage        = avg_dmg,
            strategy_mode         = mode,
            asset                 = asset if gfield == "asset" else None,
            linked_proposal_type  = prop_type.value if prop_type else None,
            source_problem_id     = pid,
            regime_concentration  = regime_conc,
            regime_label_detail   = regime_detail,
        ))

    return suggestions


def _evidence(
    count: int,
    freq: float,
    mode: Optional[str],
    asset: Optional[str],
    group_field: str,
    group_value: str,
    regime: Optional[str] = None,
) -> str:
    parts = [f"n={count}", f"{freq:.1f}% of trades"]
    if group_field not in ("primary_diagnosis",) and group_value not in ("UNKNOWN",):
        parts.append(f"{group_field}={group_value}")
    if mode and mode != "UNKNOWN":
        parts.append(f"mode={mode}")
    if asset and asset != "UNKNOWN" and group_field != "asset":
        parts.append(f"asset={asset}")
    if regime:
        parts.append(f"regime={regime}")
    return " | ".join(parts)


# ── Escalation-to-proposal support ───────────────────────────────────────────

def suggestion_to_proposal_input(
    suggestion: RemediationSuggestion,
) -> Optional[dict]:
    """Convert an escalatable suggestion into a ProposalRecord-compatible dict.

    Returns None when the suggestion's action type is purely operational
    (no configuration parameter to propose).

    The returned dict is safe to unpack as ``ProposalRecord(**result)`` kwargs.
    Human review, backtest, paper validation, and approval are still required —
    this function only prepares the draft input.  It does NOT create, save,
    or advance any proposal automatically.

    Parameters
    ----------
    suggestion:
        A :class:`RemediationSuggestion` returned by
        :func:`generate_remediation_suggestions`.

    Returns
    -------
    Dict of ProposalRecord constructor kwargs, or None.
    """
    if not suggestion.is_escalatable:
        return None

    return {
        "proposal_type":    suggestion.linked_proposal_type,
        "strategy_mode":    suggestion.strategy_mode,
        "asset":            suggestion.asset,
        "asset_class":      suggestion.asset_class,
        "macro_regime":     suggestion.regime_concentration,
        "current_value":    None,   # operator must fill from live config
        "proposed_value":   None,   # operator must fill with target value
        "reason_summary":   suggestion.reason_summary[:200],
        "evidence_summary": suggestion.evidence_summary,
        "evidence_metrics": {
            "source":           "diagnosis_aggregator",
            "diagnosis_category": suggestion.diagnosis_category,
            "count":            suggestion.count,
            "frequency_pct":    suggestion.frequency_pct,
            "total_pnl_damage": suggestion.total_pnl_damage,
            "avg_pnl_damage":   suggestion.avg_pnl_damage,
            "suggestion_id":    suggestion.suggestion_id,
            "regime_concentration": suggestion.regime_concentration,
            "regime_label_detail":  suggestion.regime_label_detail,
        },
        # Approval status always starts at draft — state machine enforces this
        "approval_status":  "draft",
    }


# ── Convenience ranking of suggestions ───────────────────────────────────────

def rank_suggestions(
    suggestions: list[RemediationSuggestion],
    by: str = "priority",
) -> list[RemediationSuggestion]:
    """Return suggestions sorted by *by*.

    Parameters
    ----------
    by:
        ``"priority"`` (high→medium→low), ``"frequency"``, ``"pnl_damage"``,
        ``"count"``.
    """
    _PRIORITY_ORDER = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}

    if by == "priority":
        return sorted(suggestions, key=lambda s: _PRIORITY_ORDER.get(s.escalation_priority, 9))
    if by == "frequency":
        return sorted(suggestions, key=lambda s: -s.frequency_pct)
    if by == "pnl_damage":
        return sorted(suggestions, key=lambda s: s.total_pnl_damage)   # negative = worse first
    if by == "count":
        return sorted(suggestions, key=lambda s: -s.count)
    raise ValueError(f"Invalid rank field {by!r}. Valid: priority, frequency, pnl_damage, count")
