"""Prefilter layer — volume gate applied *before* the full signal engine.

Only the volume expansion gate is active.  All symbols that pass the
volume check proceed to signal evaluation; no rank cutoff is applied.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.config import (
    PREFILTER_VOLUME_EXPANSION_NORMAL,
)
from src.scanner.asset_universe import get_entry

log = logging.getLogger(__name__)


# ── Skip reason codes ─────────────────────────────────────────────────────────

SKIP_WEAK_VOLUME = "blocked_by_weak_volume"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PrefilterResult:
    """Outcome of the prefilter pipeline for one symbol."""
    symbol:          str
    passed:          bool  = True
    skip_reason:     str   = ""
    atr_pct:         float = 0.0
    volume_ratio:    float = 0.0    # current_vol / avg_vol
    avg_volume:      float = 0.0    # 20-bar average volume
    rank_score:      float = 0.0    # kept for logging compatibility
    universe_group:  Optional[str] = None
    is_meme:         bool  = False


# ── Volume gate ───────────────────────────────────────────────────────────────

def check_volume(volume_ratio: float) -> tuple[bool, str]:
    """Return (passed, skip_reason) for volume expansion gate."""
    if volume_ratio < PREFILTER_VOLUME_EXPANSION_NORMAL:
        return False, SKIP_WEAK_VOLUME
    return True, ""


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_prefilter(
    symbol: str,
    atr_pct: float,
    volume_ratio: float,
    avg_volume: float,
    mode: str,
    alligator_spread: float = 0.0,
) -> PrefilterResult:
    """Run the prefilter pipeline for one symbol (volume gate only).

    Returns a PrefilterResult with ``passed`` set to False only when
    volume expansion is insufficient.
    """
    entry = get_entry(symbol)
    ug = entry.universe_group.value if entry else None

    result = PrefilterResult(
        symbol=symbol,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        avg_volume=avg_volume,
        universe_group=ug,
    )

    # Volume expansion gate
    ok, reason = check_volume(volume_ratio)
    if not ok:
        result.passed = False
        result.skip_reason = reason

    return result
