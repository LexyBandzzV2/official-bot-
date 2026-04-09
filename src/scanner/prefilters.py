"""Prefilter layer — multi-stage gating applied *before* the full signal engine.

Stages (applied in order):
  1. Volatility gate  — ATR% must meet per-mode minimum
  2. Momentum / volume gate — recent volume must show expansion
  3. Meme-coin lane   — stricter ATR% + volume + liquidity for meme assets
  4. Top-candidate ranking — composite score, keep top-N

Each gate returns a ``PrefilterResult`` that records the decision + skip reason
so the funnel reporter can reconstruct what happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.config import (
    PREFILTER_ATR_MIN_SCALP,
    PREFILTER_ATR_MIN_INTERMEDIATE,
    PREFILTER_ATR_MIN_SWING,
    PREFILTER_VOLUME_EXPANSION_NORMAL,
    PREFILTER_VOLUME_EXPANSION_WEAK,
    PREFILTER_MEME_ATR_MIN,
    PREFILTER_MEME_VOLUME_MIN,
    PREFILTER_MEME_AVG_VOLUME_FLOOR,
    PREFILTER_TOP_N,
    PREFILTER_QUALITY_FIRST,
)
from src.scanner.asset_universe import is_meme, get_entry, UniverseGroup
from src.signals.strategy_mode import StrategyMode

log = logging.getLogger(__name__)


# ── Skip reason codes ─────────────────────────────────────────────────────────

SKIP_LOW_VOLATILITY        = "blocked_by_low_volatility"
SKIP_WEAK_VOLUME           = "blocked_by_weak_volume"
SKIP_MEME_LOW_VOLATILITY   = "blocked_memecoin_low_volatility"
SKIP_MEME_WEAK_VOLUME      = "blocked_memecoin_weak_volume"
SKIP_MEME_LOW_LIQUIDITY    = "blocked_memecoin_low_liquidity"
SKIP_BELOW_RANK_CUTOFF     = "blocked_below_rank_cutoff"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PrefilterResult:
    """Outcome of the prefilter pipeline for one symbol."""
    symbol:          str
    passed:          bool  = True
    skip_reason:     str   = ""
    atr_pct:         float = 0.0
    volume_ratio:    float = 0.0    # current_vol / avg_vol
    avg_volume:      float = 0.0    # 20-bar average volume (for meme liquidity check)
    rank_score:      float = 0.0    # composite ranking score
    universe_group:  Optional[str] = None
    is_meme:         bool  = False


# ── ATR% thresholds per strategy mode ─────────────────────────────────────────

def _atr_threshold(mode: str) -> float:
    """Return the minimum ATR% for the given strategy mode string."""
    m = mode.upper()
    if m == "SCALP":
        return PREFILTER_ATR_MIN_SCALP
    if m == "INTERMEDIATE":
        return PREFILTER_ATR_MIN_INTERMEDIATE
    if m in ("SWING", "POSITION"):
        return PREFILTER_ATR_MIN_SWING
    # Unknown mode — use the most lenient threshold
    return PREFILTER_ATR_MIN_SCALP


# ── Individual gates ──────────────────────────────────────────────────────────

def check_volatility(atr_pct: float, mode: str) -> tuple[bool, str]:
    """Return (passed, skip_reason) for ATR% volatility gate."""
    threshold = _atr_threshold(mode)
    if atr_pct < threshold:
        return False, SKIP_LOW_VOLATILITY
    return True, ""


def check_volume(volume_ratio: float, atr_pct: float, mode: str) -> tuple[bool, str]:
    """Return (passed, skip_reason) for volume expansion gate.

    Uses the *normal* expansion threshold.  If ATR is strong but volume weak,
    use the *weaker* threshold.
    """
    threshold = _atr_threshold(mode)
    if atr_pct >= threshold * 1.5:
        # Strong ATR — accept with weaker volume bar
        needed = PREFILTER_VOLUME_EXPANSION_WEAK
    else:
        needed = PREFILTER_VOLUME_EXPANSION_NORMAL

    if volume_ratio < needed:
        return False, SKIP_WEAK_VOLUME
    return True, ""


def check_meme_lane(
    atr_pct: float,
    volume_ratio: float,
    avg_volume: float,
) -> tuple[bool, str]:
    """Stricter gates for meme-coin symbols.

    Applies on top of the regular volatility/volume gates.
    """
    if atr_pct < PREFILTER_MEME_ATR_MIN:
        return False, SKIP_MEME_LOW_VOLATILITY
    if volume_ratio < PREFILTER_MEME_VOLUME_MIN:
        return False, SKIP_MEME_WEAK_VOLUME
    if avg_volume < PREFILTER_MEME_AVG_VOLUME_FLOOR:
        return False, SKIP_MEME_LOW_LIQUIDITY
    return True, ""


# ── Composite ranking score ──────────────────────────────────────────────────

def compute_rank_score(atr_pct: float, volume_ratio: float, alligator_spread: float = 0.0) -> float:
    """Compute a lightweight composite score for candidate ranking.

    Weights: 40% ATR%, 30% volume ratio, 30% alligator spread.
    """
    return (atr_pct * 0.40) + (min(volume_ratio, 3.0) * 0.30) + (alligator_spread * 0.30)


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_prefilter(
    symbol: str,
    atr_pct: float,
    volume_ratio: float,
    avg_volume: float,
    mode: str,
    alligator_spread: float = 0.0,
) -> PrefilterResult:
    """Run the full prefilter pipeline for one symbol.

    Returns a PrefilterResult with ``passed`` set based on all gates.
    """
    entry = get_entry(symbol)
    ug = entry.universe_group.value if entry else None
    meme = is_meme(symbol)

    result = PrefilterResult(
        symbol=symbol,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        avg_volume=avg_volume,
        universe_group=ug,
        is_meme=meme,
    )

    # Gate 1: volatility
    ok, reason = check_volatility(atr_pct, mode)
    if not ok:
        result.passed = False
        result.skip_reason = reason
        return result

    # Gate 2: volume expansion
    ok, reason = check_volume(volume_ratio, atr_pct, mode)
    if not ok:
        result.passed = False
        result.skip_reason = reason
        return result

    # Gate 3: meme-coin lane (extra strictness)
    if meme:
        ok, reason = check_meme_lane(atr_pct, volume_ratio, avg_volume)
        if not ok:
            result.passed = False
            result.skip_reason = reason
            return result

    # Quality-first mode: raise all thresholds by 50%
    if PREFILTER_QUALITY_FIRST:
        strict_threshold = _atr_threshold(mode) * 1.5
        if atr_pct < strict_threshold:
            result.passed = False
            result.skip_reason = SKIP_LOW_VOLATILITY
            return result

    # Compute rank score
    result.rank_score = compute_rank_score(atr_pct, volume_ratio, alligator_spread)

    return result


def select_top_candidates(
    results: list[PrefilterResult],
    top_n: Optional[int] = None,
) -> list[PrefilterResult]:
    """From a list of *passed* PrefilterResults, keep the top-N by rank_score.

    Symbols that failed an earlier gate are excluded.  If *top_n* is None the
    config default ``PREFILTER_TOP_N`` is used.
    """
    cap = top_n if top_n is not None else PREFILTER_TOP_N
    passed = [r for r in results if r.passed]
    passed.sort(key=lambda r: r.rank_score, reverse=True)

    selected = passed[:cap]
    selected_syms = {r.symbol for r in selected}

    # Mark those that passed gates but fell below the rank cutoff
    for r in results:
        if r.passed and r.symbol not in selected_syms:
            r.passed = False
            r.skip_reason = SKIP_BELOW_RANK_CUTOFF

    return selected
