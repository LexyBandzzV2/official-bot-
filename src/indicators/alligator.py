"""Alexis Alligator (Pine-aligned) — chart OHLC, SMMA on median price, no plot offset.

Matches indicator "Alexis Alligator Entry Exit Signals" (Pine v5):
    medianPrice = (high + low) / 2
    jawRaw   = smma(medianPrice, jawLength)   default 13
    teethRaw = smma(medianPrice, teethLength) default 8
    lipsRaw  = smma(medianPrice, lipsLength)  default 5

Logic uses raw lines (no jaw/teeth/lips offset).

Long entry (when USE_FRACTAL_FILTER True, default):
    bullAligned, priceAboveAlligator, breakout above last up-fractal high
Else:
    ta.crossover(close, lips)

Short entry:
    bearAligned, priceBelowAlligator, breakout below last down-fractal low
Else:
    ta.crossunder(close, lips)

Input DataFrame must include standard ``open``, ``high``, ``low``, ``close`` (chart candles).
Heikin Ashi columns may also be present for entry price display elsewhere.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Pine default: useFractalFilter = input.bool(true, ...)
USE_FRACTAL_FILTER_DEFAULT = True

JAW_LENGTH = 13
TEETH_LENGTH = 8
LIPS_LENGTH = 5


def _smma(values: np.ndarray, period: int) -> np.ndarray:
    """SMMA — seed with SMA of first `period` bars, then recursive step (Pine smma)."""
    n = len(values)
    result = np.full(n, np.nan)
    if n < period:
        return result
    result[period - 1] = np.mean(values[:period])
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return result


def calculate_alligator(
    df: pd.DataFrame,
    jaw_length: int = JAW_LENGTH,
    teeth_length: int = TEETH_LENGTH,
    lips_length: int = LIPS_LENGTH,
) -> pd.DataFrame:
    """Add jaw, teeth, lips (raw SMMA), last_up_fractal, last_down_fractal from chart OHLC."""
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    median_price = (h + lo) / 2.0

    jaw_raw = _smma(median_price, jaw_length)
    teeth_raw = _smma(median_price, teeth_length)
    lips_raw = _smma(median_price, lips_length)

    n = len(df)
    last_up = np.full(n, np.nan)
    last_dn = np.full(n, np.nan)
    lu_val = np.nan
    ld_val = np.nan

    for i in range(n):
        if i >= 4:
            if (
                h[i - 2] > h[i - 1]
                and h[i - 2] > h[i]
                and h[i - 2] > h[i - 3]
                and h[i - 2] > h[i - 4]
            ):
                lu_val = h[i - 2]
            if (
                lo[i - 2] < lo[i - 1]
                and lo[i - 2] < lo[i]
                and lo[i - 2] < lo[i - 3]
                and lo[i - 2] < lo[i - 4]
            ):
                ld_val = lo[i - 2]
        last_up[i] = lu_val
        last_dn[i] = ld_val

    out = df.copy()
    out["jaw"] = jaw_raw
    out["teeth"] = teeth_raw
    out["lips"] = lips_raw
    out["last_up_fractal"] = last_up
    out["last_down_fractal"] = last_dn
    return out


def _finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and np.isnan(x))


def alligator_buy_event(
    prev: pd.Series,
    curr: pd.Series,
    use_fractal_filter: bool = USE_FRACTAL_FILTER_DEFAULT,
) -> bool:
    """Pine longEntryRaw: bull alignment + price above gator + fractal or crossover(close,lips)."""
    for col in ("jaw", "teeth", "lips"):
        if np.isnan(curr[col]):
            return False

    lips, teeth, jaw = curr["lips"], curr["teeth"], curr["jaw"]
    bull_aligned = lips > teeth and teeth > jaw
    if not bull_aligned:
        return False

    c = float(curr["close"])
    price_above = c > lips and c > teeth and c > jaw
    if not price_above:
        return False

    if use_fractal_filter:
        lu = curr["last_up_fractal"]
        if not _finite(lu):
            return False
        return c > float(lu)

    if np.isnan(prev["close"]) or np.isnan(prev["lips"]):
        return False
    return float(prev["close"]) <= float(prev["lips"]) and c > lips


def alligator_sell_event(
    prev: pd.Series,
    curr: pd.Series,
    use_fractal_filter: bool = USE_FRACTAL_FILTER_DEFAULT,
) -> bool:
    """Pine shortEntryRaw: bear alignment + price below gator + fractal or crossunder(close,lips)."""
    for col in ("jaw", "teeth", "lips"):
        if np.isnan(curr[col]):
            return False

    lips, teeth, jaw = curr["lips"], curr["teeth"], curr["jaw"]
    bear_aligned = lips < teeth and teeth < jaw
    if not bear_aligned:
        return False

    c = float(curr["close"])
    price_below = c < lips and c < teeth and c < jaw
    if not price_below:
        return False

    if use_fractal_filter:
        ld = curr["last_down_fractal"]
        if not _finite(ld):
            return False
        return c < float(ld)

    if np.isnan(prev["close"]) or np.isnan(prev["lips"]):
        return False
    return float(prev["close"]) >= float(prev["lips"]) and c < lips


def check_alligator_buy(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return alligator_buy_event(prev, curr)


def check_alligator_sell(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return alligator_sell_event(prev, curr)


def check_lips_touch_teeth_down(df: pd.DataFrame) -> bool:
    """Pine longExitRaw: crossunder(lips, teeth) (mintick proximity omitted)."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["lips"] >= prev["teeth"]) and (curr["lips"] < curr["teeth"])


def check_lips_touch_teeth_up(df: pd.DataFrame) -> bool:
    """Pine shortExitRaw: crossover(lips, teeth)."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["lips"] <= prev["teeth"]) and (curr["lips"] > curr["teeth"])
