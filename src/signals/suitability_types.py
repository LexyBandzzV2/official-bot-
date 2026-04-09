"""Phase 14 — Suitability type contracts.

Defines the lightweight data objects that flow through the live decision
pipeline so every gating decision is transparent and auditable.

Design rules
------------
* All objects are plain dataclasses — not dicts — so attribute access is
  safe and IDE-friendly.
* No DB or config imports at module level (safe to import anywhere).
* All fields carry safe defaults so code written before Phase 14 continues
  to work unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── Suitability rating enum ───────────────────────────────────────────────────

class SuitabilityRating(str, Enum):
    """Ordered suitability levels.  HIGH is best; BLOCKED means hard veto."""

    HIGH    = "HIGH"
    MEDIUM  = "MEDIUM"
    LOW     = "LOW"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"     # no profile/proposal backing for this combination

    def allows_entry(self) -> bool:
        """True unless the rating is an explicit hard block."""
        return self is not SuitabilityRating.BLOCKED

    def friction_level(self) -> int:
        """0 = none, 1 = light, 2 = moderate, 3 = heavy, 4 = blocked."""
        return {
            SuitabilityRating.HIGH:    0,
            SuitabilityRating.MEDIUM:  2,
            SuitabilityRating.LOW:     3,
            SuitabilityRating.BLOCKED: 4,
            SuitabilityRating.UNKNOWN: 0,
        }[self]


# ── Skip / gating reason codes ────────────────────────────────────────────────

class SkipReason:
    """Structured skip-reason code constants.

    These values are persisted on signal rows (skip_reason_code column)
    and appear as prefix tokens in rejection_reason so existing logs and
    dashboards stay readable.
    """

    # Hard blocks
    BLOCKED_BY_SUITABILITY      = "blocked_by_suitability"
    BLOCKED_BY_MODE_REGIME_RULE = "blocked_by_mode_regime_rule"
    BLOCKED_BY_ASSET_REGIME_RULE = "blocked_by_asset_regime_rule"

    # Soft-friction codes (signal still executed but modified)
    THRESHOLD_RAISED_BY_REGIME   = "threshold_raised_by_regime"
    PENALIZED_BY_REGIME_PROFILE  = "penalized_by_regime_profile"
    SCORE_PENALTY_APPLIED        = "score_penalty_applied"

    # Other informative codes
    SUITABILITY_UNKNOWN          = "suitability_unknown"


# ── Rule source taxonomy ─────────────────────────────────────────────────────

class RuleSource:
    """Where the applied rule came from (for the audit trail)."""

    ACTIVE_PROFILE_SNAPSHOT = "active_profile_snapshot"
    PROMOTED_PROPOSAL       = "promoted_proposal"
    DEFAULT_SYSTEM          = "default_system"


# ── Suitability context ───────────────────────────────────────────────────────

@dataclass
class SuitabilityContext:
    """Summary of the suitability view for a particular (mode × regime) combination.

    Produced by the resolver and attached to the signal object before gating.
    Does NOT contain the executable decision; see ``LiveActivationDecision``.

    Parameters
    ----------
    strategy_mode :
        "SCALP" | "INTERMEDIATE" | "SWING" | "UNKNOWN"
    macro_regime :
        MacroRegime.value string, e.g. "TRENDING", "RANGING", or None.
    regime_label :
        Detailed RegimeLabel.value string, e.g. "TRENDING_HIGH_VOL", or None.
    suitability_rating :
        SuitabilityRating enum value.
    suitability_score :
        Optional numeric score [0.0–1.0] for rank-ordering within a rating tier.
    supporting_reason :
        Human-readable explanation (one sentence).
    source_summary :
        Which learning output / profile / proposal produced this view,
        e.g. "active profile snapshot live_conservative_v2" or
        "promoted_proposal a1b2c3".
    """

    strategy_mode:      str                 = "UNKNOWN"
    macro_regime:       Optional[str]       = None
    regime_label:       Optional[str]       = None
    suitability_rating: SuitabilityRating   = SuitabilityRating.UNKNOWN
    suitability_score:  Optional[float]     = None
    supporting_reason:  str                 = ""
    source_summary:     str                 = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_mode":      self.strategy_mode,
            "macro_regime":       self.macro_regime,
            "regime_label":       self.regime_label,
            "suitability_rating": self.suitability_rating.value,
            "suitability_score":  self.suitability_score,
            "supporting_reason":  self.supporting_reason,
            "source_summary":     self.source_summary,
        }


# ── Mode activation state ─────────────────────────────────────────────────────

class ModeActivationState(str, Enum):
    """Whether a strategy mode is allowed by the current regime profile."""

    ACTIVE    = "ACTIVE"      # no friction applied; mode is fine
    PENALIZED = "PENALIZED"   # friction (threshold raise + score penalty) applied
    BLOCKED   = "BLOCKED"     # mode is hard-blocked in this regime


# ── Live activation decision ─────────────────────────────────────────────────

@dataclass
class LiveActivationDecision:
    """The resolved runtime decision for one signal evaluation.

    Produced by ``SuitabilityResolver.resolve()`` and used by the scanner
    to gate, penalize, or block a candidate trade.

    All numeric modifiers are additive so they compose cleanly with
    existing Phase 11/12 regime adjustments.

    Parameters
    ----------
    allowed :
        True = entry may proceed (subject to other gates).
        False = entry is hard-blocked by suitability rules.
    suitability_context :
        The full SuitabilityContext that drove this decision.
    mode_activation_state :
        ACTIVE | PENALIZED | BLOCKED for the resolved mode × regime combo.
    threshold_delta :
        Added to the effective regime entry minimum score (positive = stricter).
    score_penalty :
        SUBTRACTED from sig.score_total before the entry filter (positive value
        = makes score lower). Zero when suitability is HIGH or UNKNOWN.
    applied_rule_type :
        "snapshot_rule" | "promoted_proposal" | "default"
    applied_rule_source :
        RuleSource constant or specific ID string for audit.
    skip_reason_code :
        One of the SkipReason constants, or "" when entry is allowed without
        any special treatment.
    rejection_reason :
        Human-readable rejection/warning string persisted to rejection_reason
        column on signals. Empty string when no block occurred.
    trace :
        Arbitrary dict with full audit payload (serialised to
        decision_trace_json on the signal row).
    """

    allowed:               bool                = True
    suitability_context:   Optional[SuitabilityContext] = None
    mode_activation_state: ModeActivationState  = ModeActivationState.ACTIVE
    threshold_delta:       float                = 0.0
    score_penalty:         float                = 0.0
    applied_rule_type:     str                  = "default"
    applied_rule_source:   str                  = RuleSource.DEFAULT_SYSTEM
    skip_reason_code:      str                  = ""
    rejection_reason:      str                  = ""
    trace:                 dict                 = field(default_factory=dict)

    # Phase 14: active snapshot id for traceability
    active_profile_snapshot_id: Optional[str]  = None

    def to_trace_dict(self) -> dict[str, Any]:
        """Compact trace payload for decision_trace_json column."""
        ctx = self.suitability_context
        return {
            "allowed":               self.allowed,
            "suitability_rating":    ctx.suitability_rating.value if ctx else "UNKNOWN",
            "mode_activation_state": self.mode_activation_state.value,
            "threshold_delta":       round(self.threshold_delta, 3),
            "score_penalty":         round(self.score_penalty, 3),
            "applied_rule_type":     self.applied_rule_type,
            "applied_rule_source":   self.applied_rule_source,
            "skip_reason_code":      self.skip_reason_code,
            "rejection_reason":      self.rejection_reason,
            "snapshot_id":           self.active_profile_snapshot_id,
            **self.trace,
        }
