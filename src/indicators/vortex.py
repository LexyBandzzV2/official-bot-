"""Vortex — Pine "VI Signals" aligned (chart OHLC).

Pine:
    VMP = sum(abs(high - low[1]), period)
    VMM = sum(abs(low - high[1]), period)
    STR = sum(ta.atr(1), period)   # equals sum(TR) per bar for ATR(1)
    VIP = VMP / STR
    VIM = VMM / STR

Signals: ta.crossover(VIP, VIM) buy, ta.crossover(VIM, VIP) short.

Uses standard ``high``, ``low``, ``close`` columns (chart candles).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(high)
    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    return tr


def calculate_vortex(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """VIP/VIM from chart high/low/close; denominator = rolling sum(TR) == Pine sum(ta.atr(1))."""
    n = len(df)
    h = df["high"].values.astype(float)
    lo = df["low"].values.astype(float)
    c = df["close"].values.astype(float)

    tr = _true_range(h, lo, c)

    vi_plus = np.full(n, np.nan)
    vi_minus = np.full(n, np.nan)

    for i in range(period, n):
        s_tr = 0.0
        s_vm_p = 0.0
        s_vm_m = 0.0
        for j in range(i - period + 1, i + 1):
            s_tr += tr[j]
            s_vm_p += abs(h[j] - lo[j - 1])
            s_vm_m += abs(lo[j] - h[j - 1])

        if s_tr > 0:
            vi_plus[i] = s_vm_p / s_tr
            vi_minus[i] = s_vm_m / s_tr

    out = df.copy()
    out["vi_plus"] = vi_plus
    out["vi_minus"] = vi_minus
    return out


def vortex_buy_event(prev: pd.Series, curr: pd.Series) -> bool:
    """ta.crossover(VIP, VIM)."""
    for col in ("vi_plus", "vi_minus"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["vi_plus"] <= prev["vi_minus"]) and (curr["vi_plus"] > curr["vi_minus"])


def vortex_sell_event(prev: pd.Series, curr: pd.Series) -> bool:
    """ta.crossover(VIM, VIP)."""
    for col in ("vi_plus", "vi_minus"):
        if np.isnan(prev[col]) or np.isnan(curr[col]):
            return False
    return (prev["vi_minus"] <= prev["vi_plus"]) and (curr["vi_minus"] > curr["vi_plus"])


def check_vortex_buy(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return vortex_buy_event(prev, curr)


def check_vortex_sell(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    return vortex_sell_event(prev, curr)
