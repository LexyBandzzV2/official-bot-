"""3-point confluence with a rolling candle window.

After the first indicator point fires (any of Alligator / Stochastic / Vortex),
the remaining points must occur within POINT_COMPLETION_WINDOW bars (inclusive of
the bar of the first point through first_point_index + window). If not, state
resets and scanning continues.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from src.indicators.alligator  import alligator_buy_event, alligator_sell_event
from src.indicators.stochastic import stochastic_buy_event, stochastic_sell_event
from src.indicators.vortex     import vortex_buy_event, vortex_sell_event

# Bars allowed after the first point (inclusive range [first_idx, first_idx + window])
POINT_COMPLETION_WINDOW = 10


def _bits_at(
    i: int,
    ev_a: np.ndarray,
    ev_s: np.ndarray,
    ev_v: np.ndarray,
) -> List[str]:
    bits: List[str] = []
    if ev_a[i]:
        bits.append("A")
    if ev_s[i]:
        bits.append("S")
    if ev_v[i]:
        bits.append("V")
    return bits


def _build_buy_events(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(df)
    ev_a = np.zeros(n, dtype=bool)
    ev_s = np.zeros(n, dtype=bool)
    ev_v = np.zeros(n, dtype=bool)
    for i in range(1, n):
        prev, curr = df.iloc[i - 1], df.iloc[i]
        ev_a[i] = alligator_buy_event(prev, curr)
        ev_s[i] = stochastic_buy_event(prev, curr)
        ev_v[i] = vortex_buy_event(prev, curr)
    return ev_a, ev_s, ev_v


def _build_sell_events(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(df)
    ev_a = np.zeros(n, dtype=bool)
    ev_s = np.zeros(n, dtype=bool)
    ev_v = np.zeros(n, dtype=bool)
    for i in range(1, n):
        prev, curr = df.iloc[i - 1], df.iloc[i]
        ev_a[i] = alligator_sell_event(prev, curr)
        ev_s[i] = stochastic_sell_event(prev, curr)
        ev_v[i] = vortex_sell_event(prev, curr)
    return ev_a, ev_s, ev_v


def _simulate(
    ev_a: np.ndarray,
    ev_s: np.ndarray,
    ev_v: np.ndarray,
    window: int,
) -> Tuple[List[int], set[str]]:
    """Return (completion_bar_indices, got_at_end_of_series)."""
    n = len(ev_a)
    completions: List[int] = []
    first_idx: int | None = None
    got: set[str] = set()

    for i in range(1, n):
        if first_idx is not None and i > first_idx + window:
            first_idx, got = None, set()

        bits = _bits_at(i, ev_a, ev_s, ev_v)

        if first_idx is None:
            if bits:
                first_idx = i
                got = set(bits)
                if len(got) >= 3:
                    completions.append(i)
                    first_idx, got = None, set()
            continue

        got |= set(bits)
        if len(got) >= 3:
            completions.append(i)
            first_idx, got = None, set()

    return completions, got


def analyze_buy(df: pd.DataFrame, window: int = POINT_COMPLETION_WINDOW) -> Dict:
    """Single pass: completions, validity on last bar, point flags for UI."""
    n = len(df)
    if n < 2:
        return {
            "completions": [],
            "count": 0,
            "valid_last": False,
            "points": 0,
            "alligator_point": False,
            "stochastic_point": False,
            "vortex_point": False,
        }

    ev_a, ev_s, ev_v = _build_buy_events(df)
    completions, got_end = _simulate(ev_a, ev_s, ev_v, window)
    last_i = n - 1
    valid_last = bool(completions) and completions[-1] == last_i

    if valid_last:
        pts = 3
        a_pt, s_pt, v_pt = True, True, True
    else:
        pts = len(got_end)
        a_pt, s_pt, v_pt = ("A" in got_end, "S" in got_end, "V" in got_end)

    return {
        "completions": completions,
        "count": len(completions),
        "valid_last": valid_last,
        "points": pts,
        "alligator_point": a_pt,
        "stochastic_point": s_pt,
        "vortex_point": v_pt,
    }


def analyze_sell(df: pd.DataFrame, window: int = POINT_COMPLETION_WINDOW) -> Dict:
    n = len(df)
    if n < 2:
        return {
            "completions": [],
            "count": 0,
            "valid_last": False,
            "points": 0,
            "alligator_point": False,
            "stochastic_point": False,
            "vortex_point": False,
        }

    ev_a, ev_s, ev_v = _build_sell_events(df)
    completions, got_end = _simulate(ev_a, ev_s, ev_v, window)
    last_i = n - 1
    valid_last = bool(completions) and completions[-1] == last_i

    if valid_last:
        pts = 3
        a_pt, s_pt, v_pt = True, True, True
    else:
        pts = len(got_end)
        a_pt, s_pt, v_pt = ("A" in got_end, "S" in got_end, "V" in got_end)

    return {
        "completions": completions,
        "count": len(completions),
        "valid_last": valid_last,
        "points": pts,
        "alligator_point": a_pt,
        "stochastic_point": s_pt,
        "vortex_point": v_pt,
    }
