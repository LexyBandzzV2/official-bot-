"""Phase 14 — SuitabilityResolver.

Three-layer fallback resolution chain:
  1. Active profile snapshot rules  (DB: active_profile_rules)
  2. Promoted proposals             (DB: optimization_proposals WHERE approval_status='promoted')
  3. Default system                 (no friction — fail-open)

All errors are caught; the resolver always returns a *LiveActivationDecision*.
Importing this module never raises: DB access happens lazily inside ``resolve()``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_macro_regime(regime_ctx: Any) -> Optional[str]:
    """Extract MacroRegime string from a regime_ctx object, or None."""
    if regime_ctx is None:
        return None
    mr = getattr(regime_ctx, "macro_regime", None)
    if mr is None:
        return None
    return mr.value if hasattr(mr, "value") else str(mr)


def _get_regime_label(regime_ctx: Any) -> Optional[str]:
    """Extract RegimeLabel string from a regime_ctx object, or None."""
    if regime_ctx is None:
        return None
    rl = getattr(regime_ctx, "regime_label", None)
    if rl is None:
        return None
    return rl.value if hasattr(rl, "value") else str(rl)


def _default_decision() -> "LiveActivationDecision":
    """Return a permissive (fail-open) LiveActivationDecision."""
    from src.signals.suitability_types import (
        LiveActivationDecision, ModeActivationState,
        RuleSource, SuitabilityContext, SuitabilityRating,
    )
    ctx = SuitabilityContext(suitability_rating=SuitabilityRating.UNKNOWN)
    return LiveActivationDecision(
        allowed=True,
        suitability_context=ctx,
        mode_activation_state=ModeActivationState.ACTIVE,
        threshold_delta=0.0,
        score_penalty=0.0,
        applied_rule_type="default",
        applied_rule_source=RuleSource.DEFAULT_SYSTEM,
    )


def _decision_from_rule(
    rule: dict,
    source_type: str,
    snapshot_id: Optional[str],
) -> "LiveActivationDecision":
    """Build a LiveActivationDecision from a db rule or proposal-shaped dict."""
    from src.signals.suitability_types import (
        LiveActivationDecision, ModeActivationState,
        RuleSource, SkipReason, SuitabilityContext, SuitabilityRating,
    )
    import src.config as cfg

    raw_rating   = (rule.get("suitability_rating") or "UNKNOWN").upper()
    try:
        rating = SuitabilityRating(raw_rating)
    except ValueError:
        rating = SuitabilityRating.UNKNOWN

    raw_mas = (rule.get("mode_activation_state") or "ACTIVE").upper()
    try:
        mas = ModeActivationState(raw_mas)
    except ValueError:
        mas = ModeActivationState.ACTIVE

    block_entry: bool = bool(rule.get("block_entry", 0))

    # --- compute friction deltas -------------------------------------------------
    th_delta: float   = 0.0
    sc_penalty: float = 0.0

    if cfg.SUITABILITY_GATING_ENABLED:
        override_th    = float(rule.get("threshold_delta") or 0.0)
        override_sc    = float(rule.get("score_penalty") or 0.0)

        if override_th != 0.0 or override_sc != 0.0:
            # Explicit rule overrides; still respect the global enable flags
            if cfg.SUITABILITY_THRESHOLD_RAISE_ENABLED:
                th_delta = override_th
            if cfg.SUITABILITY_SCORE_PENALTY_ENABLED:
                sc_penalty = override_sc
        else:
            # No explicit override ⇒ derive from rating
            if rating == SuitabilityRating.MEDIUM:
                if cfg.SUITABILITY_THRESHOLD_RAISE_ENABLED:
                    th_delta   = cfg.SUITABILITY_MEDIUM_THRESHOLD_DELTA
                if cfg.SUITABILITY_SCORE_PENALTY_ENABLED:
                    sc_penalty = cfg.SUITABILITY_MEDIUM_SCORE_PENALTY
            elif rating == SuitabilityRating.LOW:
                if cfg.SUITABILITY_THRESHOLD_RAISE_ENABLED:
                    th_delta   = cfg.SUITABILITY_LOW_THRESHOLD_DELTA
                if cfg.SUITABILITY_SCORE_PENALTY_ENABLED:
                    sc_penalty = cfg.SUITABILITY_LOW_SCORE_PENALTY
            # HIGH / UNKNOWN ⇒ no friction

    # --- determine block ---------------------------------------------------------
    is_blocked = (
        block_entry
        or mas == ModeActivationState.BLOCKED
        or rating == SuitabilityRating.BLOCKED
    )

    # --- map mas to penalized when friction is active ----------------------------
    if not is_blocked and (th_delta > 0 or sc_penalty > 0):
        mas = ModeActivationState.PENALIZED

    # --- build context ------------------------------------------------------------
    ctx = SuitabilityContext(
        strategy_mode      = rule.get("strategy_mode") or "UNKNOWN",
        macro_regime       = rule.get("macro_regime"),
        regime_label       = rule.get("regime_label"),
        suitability_rating = rating,
        suitability_score  = rule.get("suitability_score"),
        supporting_reason  = rule.get("supporting_reason") or "",
        source_summary     = source_type,
    )

    # --- skip / rejection strings -----------------------------------------------
    skip_code = ""
    rejection  = ""
    if is_blocked:
        skip_code = SkipReason.BLOCKED_BY_SUITABILITY
        rejection = (
            f"[{skip_code}] suitability={rating.value} "
            f"mode_state={mas.value} source={source_type}"
        )
    elif sc_penalty > 0 or th_delta > 0:
        skip_code = SkipReason.SCORE_PENALTY_APPLIED

    return LiveActivationDecision(
        allowed                  = not is_blocked,
        suitability_context      = ctx,
        mode_activation_state    = mas,
        threshold_delta          = th_delta,
        score_penalty            = sc_penalty,
        applied_rule_type        = source_type,
        applied_rule_source      = (
            RuleSource.ACTIVE_PROFILE_SNAPSHOT
            if source_type == "snapshot_rule"
            else RuleSource.PROMOTED_PROPOSAL
        ),
        skip_reason_code         = skip_code,
        rejection_reason         = rejection,
        active_profile_snapshot_id = snapshot_id,
    )


# ── Rule matching ─────────────────────────────────────────────────────────────

def _match_rule(
    rules: list[dict],
    strategy_mode: Optional[str],
    macro_regime: Optional[str],
    regime_label: Optional[str],
    asset: Optional[str],
) -> Optional[dict]:
    """Return the most specific matching rule from *rules*, or None.

    Specificity order (most → least):
      1. asset + regime_label + strategy_mode
      2. asset + macro_regime + strategy_mode
      3. regime_label + strategy_mode
      4. macro_regime + strategy_mode
      5. strategy_mode
      6. regime_label
      7. macro_regime
      8. first catch-all rule (no conditions set)
    """
    def _m(r: dict, field: str, val: Optional[str]) -> bool:
        """True if rule field is blank (wildcard) OR matches val."""
        rv = r.get(field) or ""
        return rv == "" or rv == val

    candidates: list[tuple[int, dict]] = []
    for r in rules:
        m_mode   = _m(r, "strategy_mode", strategy_mode)
        m_macro  = _m(r, "macro_regime",  macro_regime)
        m_label  = _m(r, "regime_label",  regime_label)
        m_asset  = _m(r, "asset",         asset)
        if not (m_mode and m_macro and m_label and m_asset):
            continue
        # Specificity score (higher = more specific)
        score = (
            (1 if r.get("asset")         else 0) * 8
            + (1 if r.get("regime_label") else 0) * 4
            + (1 if r.get("macro_regime") else 0) * 2
            + (1 if r.get("strategy_mode") else 0) * 1
        )
        candidates.append((score, r))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ── Main resolver class ───────────────────────────────────────────────────────

class SuitabilityResolver:
    """Resolves live suitability for a signal candidate.

    Instantiate once per scanner run (it caches the active snapshot on first
    access to avoid repeated DB round-trips).  The resolver is completely
    stateless w.r.t. individual signals.

    Usage::

        resolver = SuitabilityResolver()
        for sig in candidates:
            decision = resolver.resolve(sig, regime_ctx)
            sig.live_activation_decision = decision
    """

    def __init__(self) -> None:
        self._snapshot_loaded   = False
        self._snapshot_id: Optional[str] = None
        self._rules: list[dict]           = []
        self._promoted_proposals: Optional[list[dict]] = None   # lazy

    # ------------------------------------------------------------------

    def _load_snapshot(self) -> None:
        """Load the active profile snapshot + rules (cached after first call)."""
        if self._snapshot_loaded:
            return
        self._snapshot_loaded = True
        try:
            from src.data.db import get_active_profile_snapshot, get_active_profile_rules
            snap = get_active_profile_snapshot()
            if snap:
                self._snapshot_id = snap.get("snapshot_id")
                self._rules = get_active_profile_rules(self._snapshot_id or "")
                log.debug(
                    "SuitabilityResolver: loaded snapshot %s with %d rules",
                    self._snapshot_id, len(self._rules),
                )
        except Exception as exc:
            log.debug("SuitabilityResolver: snapshot load failed, fail-open: %s", exc)

    def _load_promoted_proposals(self) -> list[dict]:
        if self._promoted_proposals is not None:
            return self._promoted_proposals
        try:
            from src.data.db import get_promoted_proposals_for_fallback
            self._promoted_proposals = get_promoted_proposals_for_fallback()
        except Exception as exc:
            log.debug("SuitabilityResolver: proposal load failed: %s", exc)
            self._promoted_proposals = []
        return self._promoted_proposals

    # ------------------------------------------------------------------

    def resolve(self, sig: Any, regime_ctx: Any) -> "LiveActivationDecision":
        """Return a LiveActivationDecision for *sig* given *regime_ctx*.

        Always returns a valid ``LiveActivationDecision``; never raises.
        """
        try:
            return self._resolve_inner(sig, regime_ctx)
        except Exception as exc:
            log.warning(
                "SuitabilityResolver.resolve raised unexpectedly (fail-open): %s", exc
            )
            return _default_decision()

    def _resolve_inner(self, sig: Any, regime_ctx: Any) -> "LiveActivationDecision":
        import src.config as cfg

        if not cfg.SUITABILITY_GATING_ENABLED:
            return _default_decision()

        strategy_mode = getattr(sig, "strategy_mode", None)
        macro_regime  = _get_macro_regime(regime_ctx)
        regime_label  = _get_regime_label(regime_ctx)
        asset         = getattr(sig, "asset", None)

        # ── Layer 1: active profile snapshot ──────────────────────────────────
        self._load_snapshot()
        if self._rules:
            rule = _match_rule(
                self._rules, strategy_mode, macro_regime, regime_label, asset
            )
            if rule is not None:
                return _decision_from_rule(rule, "snapshot_rule", self._snapshot_id)

        # ── Layer 2: promoted proposals (fallback) ────────────────────────────
        promoted = self._load_promoted_proposals()
        if promoted:
            # Normalise a proposal row to rule-shaped dict for matching
            # Proposals with threshold_delta > 0 or score_penalty > 0 carry
            # suitability friction; others are treated as HIGH suitability.
            rule = _match_rule(
                promoted, strategy_mode, macro_regime, regime_label, asset
            )
            if rule is not None:
                # Proposals store suitability_score; derive rating if missing
                if not rule.get("suitability_rating") and rule.get("suitability_score") is not None:
                    try:
                        from src.tools.profile_materializer import _score_to_rating
                        rule = dict(rule)   # copy — do not mutate the cached list
                        rule["suitability_rating"] = _score_to_rating(rule.get("suitability_score"))
                    except Exception:
                        pass
                return _decision_from_rule(rule, "promoted_proposal", None)

        # ── Layer 3: default system (fail-open) ───────────────────────────────
        return _default_decision()

    # ------------------------------------------------------------------
    # Snapshot cache invalidation (call when a new snapshot is activated)

    def reload(self) -> None:
        """Force reload of the snapshot cache on the next ``resolve()`` call."""
        self._snapshot_loaded = False
        self._snapshot_id = None
        self._rules = []
        self._promoted_proposals = None
