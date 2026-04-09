"""Phase 14 — Profile Materializer.

Converts *promoted* optimization proposals into an active profile snapshot
that the SuitabilityResolver can query at runtime.

The materializer:
  1. Loads all promoted proposals from the DB.
  2. Groups them by (macro_regime, regime_label, strategy_mode, asset).
  3. Deriving a suitability_rating per group from the proposals' numeric
     evidence (suitability_score column; falls back to "HIGH" when absent).
  4. Writes one ``active_profile_snapshots`` header row and N
     ``active_profile_rules`` detail rows.
  5. Marks the new snapshot as ``is_active=1`` and deactivates any previous
     active snapshot.

Usage
-----
    from src.tools.profile_materializer import build_snapshot_from_promoted_proposals
    snapshot_id = build_snapshot_from_promoted_proposals()
    if snapshot_id:
        print(f"New profile snapshot activated: {snapshot_id}")

This module is intentionally standalone — it can be called from a script,
a management command, or a background job.  It never modifies live gating
directly; the SuitabilityResolver picks up the new snapshot on the next
``resolve()`` call (or after ``resolver.reload()`` is called).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Suitability score → rating mapping ────────────────────────────────────────

def _score_to_rating(score: Optional[float]) -> str:
    """Convert a numeric evidence score [0.0–1.0] to a SuitabilityRating string."""
    if score is None:
        return "HIGH"
    if score >= 0.75:
        return "HIGH"
    if score >= 0.50:
        return "MEDIUM"
    if score >= 0.25:
        return "LOW"
    return "BLOCKED"


# ── Proposal → rule row ──────────────────────────────────────────────────────

def _proposal_to_rule(proposal: dict, snapshot_id: str) -> dict:
    """Derive a profile rule dict from a promoted proposal dict."""
    score = proposal.get("suitability_score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None

    rating = _score_to_rating(score)

    # Threshold / penalty from proposal (if present) or derive from rating
    try:
        threshold_delta = float(proposal.get("threshold_delta") or 0.0)
    except (TypeError, ValueError):
        threshold_delta = 0.0
    try:
        score_penalty = float(proposal.get("score_penalty") or 0.0)
    except (TypeError, ValueError):
        score_penalty = 0.0

    block_entry = int(bool(proposal.get("block_entry", 0)))

    return {
        "rule_id":              str(uuid.uuid4()),
        "snapshot_id":          snapshot_id,
        "created_at":           datetime.now(timezone.utc).isoformat(),
        "macro_regime":         proposal.get("macro_regime"),
        "regime_label":         proposal.get("regime_label"),
        "strategy_mode":        proposal.get("strategy_mode"),
        "asset":                proposal.get("asset"),
        "asset_class":          proposal.get("asset_class"),
        "suitability_rating":   rating,
        "suitability_score":    score,
        "mode_activation_state": "BLOCKED" if block_entry else (
            "PENALIZED" if (threshold_delta > 0 or score_penalty > 0) else "ACTIVE"
        ),
        "threshold_delta":      threshold_delta,
        "score_penalty":        score_penalty,
        "block_entry":          block_entry,
        "supporting_reason":    proposal.get("reason_summary") or "",
        "source_proposal_id":   proposal.get("proposal_id"),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def build_snapshot_from_promoted_proposals(
    profile_name: str = "auto_promoted",
    notes: str = "",
) -> Optional[str]:
    """Materialise an active profile snapshot from all promoted proposals.

    Returns the new ``snapshot_id`` on success, or ``None`` if there are no
    promoted proposals or the operation fails.

    Parameters
    ----------
    profile_name :
        Human-readable label stored on the snapshot header row.
    notes :
        Optional free-text annotation stored on the snapshot header.
    """
    try:
        from src.data.db import (
            get_promoted_proposals_for_fallback,
            save_profile_snapshot,
            save_profile_rule,
        )
    except Exception as exc:
        log.error("profile_materializer: DB import failed: %s", exc)
        return None

    proposals = get_promoted_proposals_for_fallback()
    if not proposals:
        log.info("profile_materializer: no promoted proposals found — nothing to materialise")
        return None

    snapshot_id  = str(uuid.uuid4())
    now_iso      = datetime.now(timezone.utc).isoformat()

    header = {
        "snapshot_id":    snapshot_id,
        "profile_name":   profile_name,
        "created_at":     now_iso,
        "activated_at":   now_iso,
        "is_active":      0,          # activated separately via activate_snapshot()
        "source_summary": f"materialised from {len(proposals)} promoted proposals",
        "notes":          notes,
    }
    save_profile_snapshot(header)
    log.info(
        "profile_materializer: created snapshot %s from %d proposals",
        snapshot_id, len(proposals),
    )

    for prop in proposals:
        rule = _proposal_to_rule(prop, snapshot_id)
        save_profile_rule(rule)

    log.debug(
        "profile_materializer: wrote %d rules into snapshot %s",
        len(proposals), snapshot_id,
    )
    return snapshot_id


def activate_snapshot(snapshot_id: str) -> bool:
    """Mark *snapshot_id* as the active profile and deactivate all others.

    Returns True on success, False on failure.
    """
    try:
        import sqlite3
        from src.data.db import _sqlite_conn  # type: ignore[attr-defined]

        with _sqlite_conn() as conn:
            conn.execute(
                "UPDATE active_profile_snapshots SET is_active=0 WHERE is_active=1"
            )
            conn.execute(
                "UPDATE active_profile_snapshots SET is_active=1, activated_at=? "
                "WHERE snapshot_id=?",
                (datetime.now(timezone.utc).isoformat(), snapshot_id),
            )
        log.info("profile_materializer: activated snapshot %s", snapshot_id)
        return True
    except Exception as exc:
        log.error("activate_snapshot failed: %s", exc)
        return False


def build_and_activate_from_promoted_proposals(
    profile_name: str = "auto_promoted",
    notes: str = "",
) -> Optional[str]:
    """Convenience: materialise a new snapshot from promoted proposals and activate it.

    Returns the activated ``snapshot_id``, or ``None`` on failure.
    """
    snapshot_id = build_snapshot_from_promoted_proposals(
        profile_name=profile_name, notes=notes
    )
    if snapshot_id and activate_snapshot(snapshot_id):
        return snapshot_id
    return None
