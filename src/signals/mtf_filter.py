"""Multi-Timeframe (MTF) Trend Filter.

Before accepting a signal on the base timeframe, check the alligator
direction on the next higher timeframe.  This eliminates counter-trend
entries — the most common cause of losing trades.

Timeframe hierarchy:
    1m  → confirm on 5m
    3m  → confirm on 15m
    5m  → confirm on 15m
    15m → confirm on 1h
    1h  → confirm on 4h
    4h  → confirm on 1d (best-effort; 1d data slower)

Return values:
    "ALIGNED"      — HTF alligator agrees with signal direction
    "COUNTER"      — HTF alligator opposes signal direction (block or reduce size)
    "NEUTRAL"      — HTF alligator tangled/indecisive (allow with normal sizing)
    "UNAVAILABLE"  — Could not fetch HTF data (fail-open: allow signal through)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# ── Timeframe hierarchy ───────────────────────────────────────────────────────
_HIGHER_TF: dict[str, str] = {
    "1m":  "5m",
    "2m":  "15m",
    "3m":  "15m",
    "5m":  "15m",
    "15m": "1h",
    "30m": "1h",
    "1h":  "4h",
    "2h":  "4h",
    "4h":  "1d",
}

def get_higher_timeframe(base_tf: str) -> Optional[str]:
    """Return the confirmation timeframe for *base_tf*, or None if at the top."""
    return _HIGHER_TF.get(base_tf)


# ── Alligator direction on a DataFrame ───────────────────────────────────────

def _alligator_direction(df: pd.DataFrame) -> str:
    """Return 'UP', 'DOWN', or 'NEUTRAL' from the last bar's alligator state.

    Expects columns: 'jaw', 'teeth', 'lips'  OR  'alligator_jaw' etc.
    Falls back to SMMA computation if columns are missing.
    """
    # Try pre-computed columns (various naming conventions)
    for jaw_col, teeth_col, lips_col in [
        ("jaw", "teeth", "lips"),
        ("alligator_jaw", "alligator_teeth", "alligator_lips"),
        ("smma13", "smma8", "smma5"),
    ]:
        if all(c in df.columns for c in (jaw_col, teeth_col, lips_col)):
            jaw   = float(df[jaw_col].iloc[-1])
            teeth = float(df[teeth_col].iloc[-1])
            lips  = float(df[lips_col].iloc[-1])
            if lips > teeth > jaw:
                return "UP"
            if lips < teeth < jaw:
                return "DOWN"
            return "NEUTRAL"

    # Compute SMMA from close if no alligator columns present
    close_col = "ha_close" if "ha_close" in df.columns else "close"
    if close_col not in df.columns or len(df) < 13:
        return "NEUTRAL"

    close = df[close_col]

    def smma(series: pd.Series, period: int) -> float:
        result = series.ewm(alpha=1.0 / period, adjust=False).mean()
        return float(result.iloc[-1])

    jaw   = smma(close, 13)
    teeth = smma(close, 8)
    lips  = smma(close, 5)

    if lips > teeth > jaw:
        return "UP"
    if lips < teeth < jaw:
        return "DOWN"
    return "NEUTRAL"


# ── Main public function ──────────────────────────────────────────────────────

def check_mtf_alignment(
    symbol:    str,
    base_tf:   str,
    direction: str,          # "buy" or "sell"
    bars:      int = 100,
) -> str:
    """Return MTF alignment status for a pending signal.

    Parameters
    ----------
    symbol:    Asset symbol (e.g. "BTCUSD", "AAPL").
    base_tf:   The signal's timeframe (e.g. "5m").
    direction: "buy" or "sell".
    bars:      How many bars to fetch on the higher timeframe.

    Returns one of: "ALIGNED", "COUNTER", "NEUTRAL", "UNAVAILABLE".
    """
    htf = get_higher_timeframe(base_tf)
    if htf is None:
        return "NEUTRAL"   # Already at highest timeframe

    try:
        from src.data.market_data import get_latest_candles
        htf_df = get_latest_candles(symbol, htf, count=bars)
        if htf_df is None or htf_df.empty:
            log.debug("MTF: no %s data for %s — skipping filter", htf, symbol)
            return "UNAVAILABLE"

        trend = _alligator_direction(htf_df)
        dir_up = direction.lower() in ("buy", "long")

        if trend == "NEUTRAL":
            return "NEUTRAL"
        if (trend == "UP" and dir_up) or (trend == "DOWN" and not dir_up):
            return "ALIGNED"
        return "COUNTER"

    except Exception as exc:
        log.debug("MTF check failed for %s %s: %s", symbol, base_tf, exc)
        return "UNAVAILABLE"
