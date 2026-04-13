"""Prefilter layer — DISABLED.

All symbols pass unconditionally.  Volume, ATR, and ML gates have been
removed.  The only gate that remains active is MTF (multi-timeframe)
alignment inside market_scanner.py, which is intentional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PrefilterResult:
    """Outcome of the prefilter pipeline for one symbol.

    Always passes — kept so imports elsewhere don't break.
    """
    symbol:         str
    passed:         bool  = True
    skip_reason:    str   = ""
    atr_pct:        float = 0.0
    volume_ratio:   float = 0.0
    avg_volume:     float = 0.0
    rank_score:     float = 0.0
    universe_group: Optional[str] = None
    is_meme:        bool  = False


def run_prefilter(
    symbol: str,
    atr_pct: float = 0.0,
    volume_ratio: float = 0.0,
    avg_volume: float = 0.0,
    mode: str = "",
    alligator_spread: float = 0.0,
) -> PrefilterResult:
    """Always returns a passing result — all gates removed."""
    return PrefilterResult(
        symbol=symbol,
        passed=True,
        skip_reason="",
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        avg_volume=avg_volume,
    )
