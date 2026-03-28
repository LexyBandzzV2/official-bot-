"""Heikin Ashi conversion — the core transformation applied to EVERY candle.

ALL candles from every data source MUST pass through convert_to_heikin_ashi()
before any indicator is calculated or any signal is evaluated.  This is
non-negotiable and enforced throughout the codebase.

Formula (matches Pine Script exactly):
    HA_Close = (open + high + low + close) / 4
    HA_Open  = (prev_HA_Open + prev_HA_Close) / 2   [first bar: (open+close)/2]
    HA_High  = max(high, HA_Open, HA_Close)
    HA_Low   = min(low,  HA_Open, HA_Close)
    HA_Body  = abs(HA_Close - HA_Open)
    is_doji  = (HA_Body / HA_Range) < 0.10
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── Public API ────────────────────────────────────────────────────────────────

def convert_to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a standard OHLCV DataFrame to Heikin Ashi candles.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns (case-insensitive): open, high, low, close.
        A ``volume`` column is preserved unchanged.
        A datetime index or a ``time`` column is expected for logging.

    Returns
    -------
    pd.DataFrame
        Original columns **plus** the following new columns:

        ha_open, ha_high, ha_low, ha_close  — Heikin Ashi OHLC
        ha_body     — absolute body size  |HA_Close - HA_Open|
        ha_range    — full candle range    HA_High - HA_Low
        is_doji     — True when body < 10 % of range
        is_bullish  — True when HA_Close > HA_Open
        is_bearish  — True when HA_Close < HA_Open
    """
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    n = len(df)
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)

    ha_close = (o + h + l + c) / 4.0
    ha_open  = np.empty(n, dtype=float)
    ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum(h, np.maximum(ha_open, ha_close))
    ha_low  = np.minimum(l, np.minimum(ha_open, ha_close))

    ha_body  = np.abs(ha_close - ha_open)
    ha_range = ha_high - ha_low

    # Doji: body is less than 10 % of range.  Zero-range candle = doji by default.
    is_doji    = np.where(ha_range > 0, ha_body / ha_range < 0.10, True)
    is_bullish = ha_close > ha_open
    is_bearish = ha_close < ha_open

    df["ha_open"]    = ha_open
    df["ha_high"]    = ha_high
    df["ha_low"]     = ha_low
    df["ha_close"]   = ha_close
    df["ha_body"]    = ha_body
    df["ha_range"]   = ha_range
    df["is_doji"]    = is_doji
    df["is_bullish"] = is_bullish
    df["is_bearish"] = is_bearish

    return df


def check_three_candle_staircase(ha_df: pd.DataFrame, direction: str) -> bool:
    """Validate the 3-candle Heikin Ashi staircase pattern.

    Rules (both buy and sell):
    - Exactly the last 3 candles are examined.
    - All 3 must be in the correct direction (all green for bull, all red for bear).
    - Body of candle 2 must be strictly larger than candle 1.
    - Body of candle 3 must be strictly larger than candle 2.
    - Zero doji candles allowed — one doji disqualifies the entire staircase.

    Parameters
    ----------
    ha_df     : DataFrame already converted to Heikin Ashi (has ha_body, is_doji columns).
    direction : ``'bull'`` or ``'bear'``

    Returns
    -------
    bool — True only when every condition is satisfied.
    """
    if len(ha_df) < 3:
        return False

    c1 = ha_df.iloc[-3]
    c2 = ha_df.iloc[-2]
    c3 = ha_df.iloc[-1]

    # Any doji disqualifies the whole staircase
    if c1["is_doji"] or c2["is_doji"] or c3["is_doji"]:
        return False

    # Bodies must be progressively larger (strict)
    if not (c2["ha_body"] > c1["ha_body"] and c3["ha_body"] > c2["ha_body"]):
        return False

    if direction == "bull":
        return bool(c1["is_bullish"] and c2["is_bullish"] and c3["is_bullish"])
    if direction == "bear":
        return bool(c1["is_bearish"] and c2["is_bearish"] and c3["is_bearish"])

    return False


def is_doji_candle(ha_open: float, ha_close: float,
                   ha_high: float, ha_low: float) -> bool:
    """Check a single Heikin Ashi candle for doji.  Body < 10 % of range."""
    body  = abs(ha_close - ha_open)
    rng   = ha_high - ha_low
    if rng == 0:
        return True
    return (body / rng) < 0.10
