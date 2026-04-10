"""Prefilter layer — minimal flat-market guard applied *before* the full signal engine.

Replaces the old strict ATR%/volume-expansion gates that were blocking valid
high-confluence signals on short timeframes (e.g. BTCUSD 3m with 0.3% ATR%).

The only thing we reject now is a truly dead/flat market:
  - ATR% < FLAT_MARKET_ATR_PCT  (default 0.03% — near-zero movement)
  - Volume = 0  (no data / asset not trading)

Everything else passes and is scored by the signal engine itself.
Meme coins get a slightly higher floor (0.05%) to filter ghost ticks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.scanner.asset_universe import is_meme, get_entry, UniverseGroup

log = logging.getLogger(__name__)

# Minimum ATR% to consider a market "alive" — below this it's essentially flat
FLAT_MARKET_ATR_PCT      = 0.03   # 0.03% — catches truly sideways/no-movement charts
FLAT_MARKET_ATR_PCT_MEME = 0.05   # slightly higher floor for meme coins

# Skip reason codes (kept for compatibility with funnel reporter)
SKIP_LOW_VOLATILITY        = "blocked_by_low_volatility"
SKIP_WEAK_VOLUME           = "blocked_by_weak_volume"
SKIP_MEME_LOW_VOLATILITY   = "blocked_memecoin_low_volatility"
SKIP_MEME_WEAK_VOLUME      = "blocked_memecoin_weak_volume"
SKIP_MEME_LOW_LIQUIDITY    = "blocked_memecoin_low_liquidity"
SKIP_BELOW_RANK_CUTOFF     = "blocked_below_rank_cutoff"


@dataclass
class PrefilterResult:
    """Outcome of the prefilter pipeline for one symbol."""
    symbol:          str
    passed:          bool  = True
    skip_reason:     str   = ""
    atr_pct:         float = 0.0
    volume_ratio:    float = 0.0
    avg_volume:      float = 0.0
    rank_score:      float = 0.0
    universe_group:  Optional[str] = None
    is_meme:         bool  = False


def compute_rank_score(atr_pct: float, volume_ratio: float, alligator_spread: float = 0.0) -> float:
    """Composite score used only for ranking — does NOT gate/block symbols."""
    return (atr_pct * 0.40) + (min(volume_ratio, 3.0) * 0.30) + (alligator_spread * 0.30)


def run_prefilter(
    symbol: str,
    atr_pct: float,
    volume_ratio: float,
    avg_volume: float,
    mode: str,
    alligator_spread: float = 0.0,
) -> PrefilterResult:
    """Run the flat-market guard for one symbol.

    Only blocks if the market is genuinely dead (ATR near zero or no volume).
    All valid moving markets pass through to the confluence signal engine.
    """
    entry = get_entry(symbol)
    ug    = entry.universe_group.value if entry else None
    meme  = is_meme(symbol)

    result = PrefilterResult(
        symbol=symbol,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        avg_volume=avg_volume,
        universe_group=ug,
        is_meme=meme,
    )

    # Gate: truly flat market (ATR near zero)
    floor = FLAT_MARKET_ATR_PCT_MEME if meme else FLAT_MARKET_ATR_PCT
    if atr_pct < floor:
        result.passed     = False
        result.skip_reason = SKIP_LOW_VOLATILITY
        log.debug("%s skipped — flat market (ATR %.4f%% < %.4f%%)", symbol, atr_pct, floor)
        return result

    # Gate: no volume at all (asset not trading / bad data)
    if volume_ratio <= 0.0 and avg_volume <= 0.0:
        result.passed     = False
        result.skip_reason = SKIP_WEAK_VOLUME
        log.debug("%s skipped — zero volume", symbol)
        return result

    # Compute rank score (for ordering candidates, not blocking)
    result.rank_score = compute_rank_score(atr_pct, volume_ratio, alligator_spread)
    return result


def select_top_candidates(
    results: list[PrefilterResult],
    top_n: Optional[int] = None,
) -> list[PrefilterResult]:
    """From a list of passed PrefilterResults, keep the top-N by rank_score.

    Uses PREFILTER_TOP_N from config if top_n is not provided.
    Symbols that failed the flat-market gate are excluded.
    """
    try:
        from src.config import PREFILTER_TOP_N
        cap = top_n if top_n is not None else PREFILTER_TOP_N
    except Exception:
        cap = top_n if top_n is not None else 40

    passed = [r for r in results if r.passed]
    passed.sort(key=lambda r: r.rank_score, reverse=True)

    selected     = passed[:cap]
    selected_syms = {r.symbol for r in selected}

    for r in results:
        if r.passed and r.symbol not in selected_syms:
            r.passed     = False
            r.skip_reason = SKIP_BELOW_RANK_CUTOFF

    return selected


# ── Compatibility shims for any code that imported the old gate functions ─────

def check_volatility(atr_pct: float, mode: str) -> tuple[bool, str]:
    """Legacy shim — always passes (flat-market check is in run_prefilter)."""
    return True, ""


def check_volume(volume_ratio: float, atr_pct: float, mode: str) -> tuple[bool, str]:
    """Legacy shim — always passes."""
    return True, ""


def check_meme_lane(
    atr_pct: float,
    volume_ratio: float,
    avg_volume: float,
) -> tuple[bool, str]:
    """Legacy shim — always passes (meme floor handled in run_prefilter)."""
    return True, ""
