"""Stochastic — Pine "Stochastic Extreme Signals Fixed" aligned (chart OHLC).

Pine:
    k = ta.sma(ta.stoch(close, high, low, periodK), smoothK)
    d = ta.sma(k, periodD)

Defaults: periodK=14, smoothK=1, periodD=3, triggers 80/20.

enteredAbove80  = (k > upper or d > upper) and (k[1] <= upper and d[1] <= upper)
enteredBelow20  = (k < lower or d < lower) and (k[1] >= lower and d[1] >= lower)

Uses standard ``close``, ``high``, ``low`` columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PERIOD_K = 14
SMOOTH_K = 1
PERIOD_D = 3
UPPER_TRIGGER = 80.0
LOWER_TRIGGER = 20.0


def calculate_stochastic(
    df: pd.DataFrame,
    k_period: int = PERIOD_K,
    smooth_k: int = SMOOTH_K,
    d_period: int = PERIOD_D,
) -> pd.DataFrame:
    """%K = SMA(ta.stoch(...), smoothK); %D = SMA(%K, d_period). Pine ta.stoch-style raw %K."""
    n = len(df)
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)

    raw_k = np.full(n, np.nan)
    for i in range(k_period - 1, n):
        ph = np.max(h[i - k_period + 1 : i + 1])
        pl = np.min(lo[i - k_period + 1 : i + 1])
        denom = ph - pl
        raw_k[i] = 50.0 if denom == 0 else ((c[i] - pl) / denom) * 100.0

    stoch_k = np.full(n, np.nan)
    if smooth_k <= 1:
        stoch_k = raw_k.copy()
    else:
        for i in range(smooth_k - 1, n):
            window = raw_k[i - smooth_k + 1 : i + 1]
            if not np.any(np.isnan(window)):
                stoch_k[i] = np.mean(window)

    stoch_d = np.full(n, np.nan)
    for i in range(d_period - 1, n):
        window = stoch_k[i - d_period + 1 : i + 1]
        if not np.any(np.isnan(window)):
            stoch_d[i] = np.mean(window)

    out = df.copy()
    out["stoch_k"] = stoch_k
    out["stoch_d"] = stoch_d
    return out


def stochastic_buy_event(prev: pd.Series, curr: pd.Series, upper: float = UPPER_TRIGGER) -> bool:
    """enteredAbove80 from Pine."""
    for col in ("stoch_k", "stoch_d"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (
        (curr["stoch_k"] > upper or curr["stoch_d"] > upper)
        and (prev["stoch_k"] <= upper and prev["stoch_d"] <= upper)
    )


def stochastic_sell_event(prev: pd.Series, curr: pd.Series, lower: float = LOWER_TRIGGER) -> bool:
    """enteredBelow20 from Pine."""
    for col in ("stoch_k", "stoch_d"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (
        (curr["stoch_k"] < lower or curr["stoch_d"] < lower)
        and (prev["stoch_k"] >= lower and prev["stoch_d"] >= lower)
    )


def check_stochastic_buy(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return stochastic_buy_event(prev, curr)


def check_stochastic_sell(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return stochastic_sell_event(prev, curr)
