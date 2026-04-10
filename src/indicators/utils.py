"""Indicator utilities — ATR and other shared helpers.

All calculations operate on Heikin Ashi candles.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_atr(ha_df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """Average True Range on Heikin Ashi candles.

    TR = max(HA_High - HA_Low, |HA_High - HA_Close[1]|, |HA_Low - HA_Close[1]|)
    ATR = Wilder's smoothing (equivalent to SMMA) of TR over `period` bars.

    Returns an array of length len(ha_df) with NaN for the warm-up bars.
    """
    n    = len(ha_df)
    ha_h = ha_df["ha_high"].values.astype(float)
    ha_l = ha_df["ha_low"].values.astype(float)
    ha_c = ha_df["ha_close"].values.astype(float)

    tr_arr = np.full(n, np.nan)
    for i in range(1, n):
        tr_arr[i] = max(
            ha_h[i] - ha_l[i],
            abs(ha_h[i] - ha_c[i - 1]),
            abs(ha_l[i] - ha_c[i - 1]),
        )

    atr = np.full(n, np.nan)
    if n < period + 1:
        return atr

    # Seed with simple mean
    atr[period] = np.mean(tr_arr[1 : period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr_arr[i]) / period

    return atr


def latest_atr(ha_df: pd.DataFrame, period: int = 14) -> float:
    """Return the most recent ATR value, or 0 if not yet calculable."""
    atr = calculate_atr(ha_df, period)
    # Find last non-NaN
    valid = atr[~np.isnan(atr)]
    return float(valid[-1]) if len(valid) > 0 else 0.0


def get_recent_extremes(
    ha_df: pd.DataFrame,
    lookback: int = 5,
) -> tuple[float, float]:
    """Return (recent_low, recent_high) over the last *lookback* HA candles.

    Used by the candle-structure trailing system to place the trail stop just
    beyond recent swing structure.

    If the DataFrame has fewer rows than *lookback*, all available rows are used.
    Returns ``(nan, nan)`` when the DataFrame is empty.
    """
    if ha_df is None or len(ha_df) == 0:
        return (float("nan"), float("nan"))
    tail = ha_df.iloc[-lookback:]
    recent_low  = float(tail["ha_low"].min())
    recent_high = float(tail["ha_high"].max())
    return recent_low, recent_high
