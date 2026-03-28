"""Williams Alligator — chart OHLC, SMMA on median price.

Lines: Lips (green), Teeth (red), Jaw (blue). Median price = (high + low) / 2.

Entry (Alligator leg)
    LONG  — first bar where lips is **above both** teeth and jaw, after it was **not**
            fully above both on the prior bar. The green line may take one bar or many
            to cross red and blue; **no signal until lips is above both** — then one
            signal on that completion bar.
    SHORT — first bar where lips is **below both** teeth and jaw, after it was **not**
            fully below both on the prior bar.

Exit (after entry, for position management)
    After LONG:  when lips **touches** teeth from above — lips was above red, now at
                 or below red (first contact / cross).
    After SHORT: when lips **touches** teeth from below — lips was below red, now at
                 or above red (pullback to red).

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


def _row_finite(row: pd.Series, cols: tuple[str, ...]) -> bool:
    for c in cols:
        if np.isnan(row[c]):
            return False
    return True


def _lips_above_both(row: pd.Series) -> bool:
    """True when lips > teeth and lips > jaw (green above red and blue)."""
    if not _row_finite(row, ("jaw", "teeth", "lips")):
        return False
    return bool(row["lips"] > row["teeth"] and row["lips"] > row["jaw"])


def _lips_below_both(row: pd.Series) -> bool:
    """True when lips < teeth and lips < jaw (green below red and blue)."""
    if not _row_finite(row, ("jaw", "teeth", "lips")):
        return False
    return bool(row["lips"] < row["teeth"] and row["lips"] < row["jaw"])


def alligator_buy_event(prev: pd.Series, curr: pd.Series) -> bool:
    """First bar where lips finishes above both teeth and jaw (long / buy Alligator point)."""
    return _lips_above_both(curr) and not _lips_above_both(prev)


def alligator_sell_event(prev: pd.Series, curr: pd.Series) -> bool:
    """First bar where lips finishes below both teeth and jaw (short Alligator point)."""
    return _lips_below_both(curr) and not _lips_below_both(prev)


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
    """Long exit: green was above red, now touches or crosses through red (down)."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return bool(prev["lips"] > prev["teeth"] and curr["lips"] <= curr["teeth"])


def check_lips_touch_teeth_up(df: pd.DataFrame) -> bool:
    """Short exit: green was below red, now touches or crosses through red (pullback up)."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    for col in ("lips", "teeth"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return bool(prev["lips"] < prev["teeth"] and curr["lips"] >= curr["teeth"])
