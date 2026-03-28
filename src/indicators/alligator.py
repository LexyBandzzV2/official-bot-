"""Williams Alligator — chart OHLC, SMMA on median price, no line offset for signals.

Lines (TradingView colors):
    Lips  (green)  — fastest SMMA on median price
    Teeth (red)    — middle
    Jaw   (blue)   — slowest

Signal definitions (your spec):
    BUY  — on this bar, lips crosses **up** through teeth **and** crosses **up** through jaw
           (green moves upward over red and blue).
    SHORT — on this bar, lips crosses **down** through teeth **and** **down** through jaw
           (green moves downward over red and blue).

Median price = (high + low) / 2.  SMMA lengths default 13 / 8 / 5 (jaw / teeth / lips).

Input DataFrame must include ``open``, ``high``, ``low``, ``close``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

JAW_LENGTH = 13
TEETH_LENGTH = 8
LIPS_LENGTH = 5


def _smma(values: np.ndarray, period: int) -> np.ndarray:
    """SMMA — seed with SMA of first `period` bars, then recursive step."""
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
    """Add jaw, teeth, lips (raw SMMA on median price)."""
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    median_price = (h + lo) / 2.0

    out = df.copy()
    out["jaw"] = _smma(median_price, jaw_length)
    out["teeth"] = _smma(median_price, teeth_length)
    out["lips"] = _smma(median_price, lips_length)
    return out


def _valid_three(prev: pd.Series, curr: pd.Series) -> bool:
    for col in ("jaw", "teeth", "lips"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return True


def alligator_buy_event(prev: pd.Series, curr: pd.Series) -> bool:
    """Lips (green) crosses upward over teeth (red) and jaw (blue) on this bar."""
    if not _valid_three(prev, curr):
        return False
    cross_up_teeth = (prev["lips"] <= prev["teeth"]) and (curr["lips"] > curr["teeth"])
    cross_up_jaw = (prev["lips"] <= prev["jaw"]) and (curr["lips"] > curr["jaw"])
    return bool(cross_up_teeth and cross_up_jaw)


def alligator_sell_event(prev: pd.Series, curr: pd.Series) -> bool:
    """Lips (green) crosses downward below teeth and jaw on this bar."""
    if not _valid_three(prev, curr):
        return False
    cross_dn_teeth = (prev["lips"] >= prev["teeth"]) and (curr["lips"] < curr["teeth"])
    cross_dn_jaw = (prev["lips"] >= prev["jaw"]) and (curr["lips"] < curr["jaw"])
    return bool(cross_dn_teeth and cross_dn_jaw)


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
    """Long exit: lips crosses down through teeth."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["lips"] >= prev["teeth"]) and (curr["lips"] < curr["teeth"])


def check_lips_touch_teeth_up(df: pd.DataFrame) -> bool:
    """Short exit: lips crosses up through teeth."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["lips"] <= prev["teeth"]) and (curr["lips"] > curr["teeth"])
