"""Candidate ranker — lightweight pre-filter before full signal analysis.

Scores every symbol in the asset catalogue using fast, cheap metrics:
  • ATR volatility score  — higher ATR relative to price = more movement
  • Alligator spread score — widely spread Alligator = trend in progress
  • Volume rank           — relative volume when available

Purpose: avoid running the full signal engine on sleepy markets.
Only the top-N candidates are passed to SignalEngine per scan cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

try:
    from src.data.heikin_ashi   import convert_to_heikin_ashi
    from src.indicators.alligator import calculate_alligator
    from src.indicators.utils    import latest_atr
except ImportError as e:
    log.error("Candidate ranker import error: %s", e)
    raise


@dataclass
class CandidateScore:
    symbol:         str
    atr_pct:        float   = 0.0    # ATR / price * 100
    alligator_spread: float = 0.0    # (jaw - lips) / price * 100 (spread of lines)
    alligator_aligned:bool  = False  # lines are in proper order (not tangled)
    volume_ratio:   float   = 0.0    # raw relative volume (last_vol / avg_vol)
    volume_score:   float   = 0.0    # normalized relative volume (0–1 for ranking)
    total_score:    float   = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.total_score = (
            self.atr_pct * 0.40
            + self.alligator_spread * 0.40
            + self.volume_score     * 0.20
        )


def score_symbol(
    symbol: str,
    timeframe: str,
    ha_df: pd.DataFrame,
) -> Optional[CandidateScore]:
    """Compute a quick candidate score for one symbol.

    Args:
        symbol:    symbol string
        timeframe: timeframe string
        ha_df:     Heikin Ashi DataFrame (already converted, min 20 bars)

    Returns:
        CandidateScore or None if data is insufficient.
    """
    if len(ha_df) < 20:
        return None

    try:
        # ATR score
        atr_raw = latest_atr(ha_df, period=14)
        price   = float(ha_df["ha_close"].iloc[-1])
        if price <= 0:
            return None
        atr_pct = (atr_raw / price * 100) if atr_raw else 0.0

        # Alligator spread + alignment
        alligator_df = calculate_alligator(ha_df)
        last = alligator_df.iloc[-1]
        jaw, teeth, lips = last.get("jaw"), last.get("teeth"), last.get("lips")
        spread = 0.0
        aligned = False
        if jaw and teeth and lips and not any(np.isnan([jaw, teeth, lips])):
            spread  = abs(jaw - lips) / price * 100
            # For uptrend: lips > teeth > jaw
            # For downtrend: jaw > teeth > lips
            aligned = (lips > teeth > jaw) or (jaw > teeth > lips)

        # Volume score — 3-bar recent average vs 20-bar median baseline
        vol_ratio = 0.0
        vol_score = 0.0
        if "volume" in ha_df.columns and len(ha_df) >= 20:
            recent_vol  = float(ha_df["volume"].tail(3).mean())
            baseline_vol = float(ha_df["volume"].tail(20).median())
            if baseline_vol > 0:
                vol_ratio = recent_vol / baseline_vol
                vol_score = min(1.0, vol_ratio)

        return CandidateScore(
            symbol            = symbol,
            atr_pct           = atr_pct,
            alligator_spread  = spread,
            alligator_aligned = aligned,
            volume_ratio      = vol_ratio,
            volume_score      = vol_score,
        )

    except Exception as e:
        log.debug("score_symbol failed for %s: %s", symbol, e)
        return None


def rank_candidates(
    scores: list[CandidateScore],
    top_n:  int = 10,
    min_atr: float = 0.0,   # no minimum — all symbols pass
) -> list[CandidateScore]:
    """Sort candidates by total_score descending.  No ATR cutoff applied.

    Args:
        scores:   list of CandidateScore objects
        top_n:    kept for API compatibility — ignored (all symbols returned)
        min_atr:  kept for API compatibility — ignored (no ATR gate)

    Returns:
        All candidates sorted by score descending.
    """
    sorted_scores = sorted(scores, key=lambda x: x.total_score, reverse=True)
    return sorted_scores
