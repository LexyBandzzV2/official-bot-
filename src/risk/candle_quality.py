"""Candle-quality and momentum-fade detection — Phase 4.

Provides per-candle body/wick metrics and multi-bar momentum-fade detection
used by the market scanner to tighten SCALP (and INTERMEDIATE) exits when
price momentum is clearly deteriorating.

Key concepts
------------
body_to_range_ratio
    abs(close - open) / (high - low).  High values (> 0.60) indicate a
    directional candle with little wicking.  Low values (< 0.35) indicate a
    spinning-top / inside-bar with unclear direction.

wick_ratio (adverse)
    Size of the adverse wick relative to total range.  E.g. for a BUY, the
    upper wick is favourable; the lower wick is adverse.  High adverse-wick
    share indicates price tried to move against the trade direction.

is_strong_candle
    Returns True if the body_to_range_ratio exceeds *threshold* (default 0.60)
    AND the candle closes in the expected direction (close > open for BUY,
    close < open for SELL).

shrinking_body_sequence
    Returns True if the last *n* candles each have a strictly smaller body
    than the one before, signalling weakening momentum.

momentum_fade_detected
    Primary entry point used by the scanner.  Requires:
      1. shrinking_body_sequence over the last *window* bars, AND
      2. the final (most recent) bar's body_to_range_ratio < 0.40.

    Returns False when fewer than *window* candles are provided or when the
    window is 0 (disabled mode).

Candle tuple format
-------------------
All functions accept candles as ``tuple[float, float, float, float]``
in ``(open, high, low, close)`` order — consistent with HA candle columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


Candle = tuple[float, float, float, float]   # (open, high, low, close)


# ── Per-candle metrics ────────────────────────────────────────────────────────

def body_to_range_ratio(open_: float, high: float, low: float, close: float) -> float:
    """Return abs(close - open) / (high - low).

    Returns 0.0 when high == low (degenerate candle with zero range).
    Result is clamped to [0.0, 1.0].
    """
    total_range = high - low
    if total_range <= 0.0:
        return 0.0
    return min(1.0, abs(close - open_) / total_range)


def wick_ratio(
    open_: float,
    high: float,
    low: float,
    close: float,
    direction: str,
) -> float:
    """Return the adverse-wick fraction of total candle range.

    For a BUY trade the *adverse* wick is the lower wick (price moved against
    you from the open down to the low before recovering).
    For a SELL trade the adverse wick is the upper wick.

    Returns 0.0 when total range is zero.
    """
    total_range = high - low
    if total_range <= 0.0:
        return 0.0
    dir_upper = direction.upper()
    if dir_upper == "BUY":
        adverse = min(open_, close) - low           # lower wick
    else:
        adverse = high - max(open_, close)          # upper wick
    return max(0.0, adverse / total_range)


def is_strong_candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    direction: str,
    threshold: float = 0.60,
) -> bool:
    """Return True if this candle is strongly directional for *direction*.

    Conditions:
      - body_to_range_ratio > threshold (default 0.60)
      - close is in the correct direction vs open
        (close > open for BUY, close < open for SELL)
    """
    body_ratio = body_to_range_ratio(open_, high, low, close)
    if body_ratio <= threshold:
        return False
    if direction.upper() == "BUY":
        return close > open_
    return close < open_


# ── Multi-bar sequence helpers ────────────────────────────────────────────────

def _body_size(candle: Candle) -> float:
    return abs(candle[3] - candle[0])   # abs(close - open)


def shrinking_body_sequence(candles: Sequence[Candle], n: int = 2) -> bool:
    """Return True if each of the last *n* candles has a strictly smaller body
    than the candle before it.

    Requires at least *n + 1* candles in *candles* (the extra candle provides
    the baseline for the first comparison).  Returns False when the sequence is
    too short or *n* is less than 1.
    """
    if n < 1 or len(candles) < n + 1:
        return False
    # Consider the last (n+1) candles: index -(n+1) .. -1
    window = list(candles[-(n + 1):])
    for i in range(1, len(window)):
        if _body_size(window[i]) >= _body_size(window[i - 1]):
            return False
    return True


def consecutive_strong_count(
    candles: Sequence[Candle],
    direction: str,
    threshold: float = 0.60,
) -> int:
    """Return the number of consecutive strong directional candles at the tail
    of *candles* (counting back from the most recent).
    """
    count = 0
    for candle in reversed(candles):
        o, h, l, c = candle
        if is_strong_candle(o, h, l, c, direction, threshold):
            count += 1
        else:
            break
    return count


# ── Primary momentum-fade detector ───────────────────────────────────────────

def momentum_fade_detected(
    candles: Sequence[Candle],
    direction: str,
    window: int = 3,
) -> bool:
    """Return True when momentum is clearly fading over the last *window* bars.

    Conditions (both must hold):
      1. ``shrinking_body_sequence(candles, n=window)`` — bodies contracted
         bar-over-bar across the entire evaluation window.
      2. Most-recent candle's ``body_to_range_ratio < 0.40`` — last bar is
         indecisive / nearly a doji.

    Returns False when:
      - *window* is 0 (disabled) or < 1
      - Fewer than *window + 1* candles are available
      - Conditions are not met
    """
    if window < 1 or len(candles) < window + 1:
        return False
    last = candles[-1]
    o, h, l, c = last
    if body_to_range_ratio(o, h, l, c) >= 0.40:
        return False
    return shrinking_body_sequence(candles, n=window)


# ── Dataclass for per-bar quality snapshot (optional structured output) ───────

@dataclass
class CandleQuality:
    """Snapshot of candle-quality metrics for a single bar.

    Populated by the scanner when per-bar momentum assessment is needed for
    lifecycle logging or forensic forensic_report diagnosis.
    """
    body_ratio:   float   # body_to_range_ratio result
    wick_ratio_adverse: float   # adverse-wick fraction
    is_strong:    bool
    is_fade:      bool   # momentum_fade_detected result for the current window


# ── Phase 6: structured fade evaluation ──────────────────────────────────────

@dataclass
class FadeAnalysis:
    """Structured result from :func:`evaluate_fade` for one evaluation window.

    Provides all candle-strength metrics the scanner needs to make and log a
    fade-tightening decision.  ``fade_detected`` is the actionable verdict;
    all other fields are evidence supporting it.

    Attributes
    ----------
    body_ratios:
        ``body_to_range_ratio`` for each candle in the evaluation window,
        oldest first.
    wick_ratios_adverse:
        Adverse-wick fraction for each candle in the window, oldest first.
    consecutive_strong_candles:
        Number of consecutive strong directional candles reading back from
        the most-recent bar.  Non-zero means momentum is still present.
    shrinking_body_sequence:
        True when each of the last *window* bodies contracted vs the prior bar.
    weak_candles_in_window:
        Count of bars in the window where ``body_ratio < weak_body_threshold``.
    confirmation_bars_met:
        True when ``weak_candles_in_window >= confirmation_bars``.
    fade_detected:
        Final tightening verdict: ``shrinking_body_sequence AND
        confirmation_bars_met``.  The scanner should only tighten when True.
    last_body_ratio:
        ``body_to_range_ratio`` of the most-recent bar.
    last_wick_ratio_adverse:
        Adverse-wick fraction of the most-recent bar.
    last_is_strong:
        Whether the most-recent bar qualifies as strongly directional.
    """
    body_ratios:                list[float]
    wick_ratios_adverse:        list[float]
    consecutive_strong_candles: int
    shrinking_body_sequence:    bool
    weak_candles_in_window:     int
    confirmation_bars_met:      bool
    fade_detected:              bool
    last_body_ratio:            float
    last_wick_ratio_adverse:    float
    last_is_strong:             bool

    def evidence_summary(self) -> str:
        """Compact one-line string for lifecycle-event notes.

        Example::

            "fade=True body=[0.82,0.55,0.28] wick=[0.02,0.08,0.15] strong=0 confirm=2/3"
        """
        body_s = "[" + ",".join(f"{r:.2f}" for r in self.body_ratios) + "]"
        wick_s = "[" + ",".join(f"{r:.2f}" for r in self.wick_ratios_adverse) + "]"
        n = len(self.body_ratios)
        return (
            f"fade={self.fade_detected} "
            f"body={body_s} wick={wick_s} "
            f"strong={self.consecutive_strong_candles} "
            f"confirm={self.weak_candles_in_window}/{n}"
        )


_FADE_EMPTY = FadeAnalysis(
    body_ratios=[], wick_ratios_adverse=[], consecutive_strong_candles=0,
    shrinking_body_sequence=False, weak_candles_in_window=0,
    confirmation_bars_met=False, fade_detected=False,
    last_body_ratio=0.0, last_wick_ratio_adverse=0.0, last_is_strong=False,
)


def evaluate_fade(
    candles: Sequence[Candle],
    direction: str,
    *,
    window: int = 3,
    weak_body_threshold: float = 0.40,
    strong_body_threshold: float = 0.60,
    adverse_wick_threshold: float = 0.30,  # stored in FadeAnalysis; informational only
    confirmation_bars: int = 1,
) -> FadeAnalysis:
    """Evaluate candle-strength over the last *window* bars.

    Parameters
    ----------
    candles:
        Recent ``(open, high, low, close)`` tuples, oldest first.
    direction:
        ``"BUY"`` or ``"SELL"``.
    window:
        Number of bars in the evaluation window (must be ≥ 1).  Pass 0 to
        receive a *fade_detected=False* placeholder.
    weak_body_threshold:
        Body ratio below which a candle counts as weak / indecisive.
        Default 0.40 matches the original ``momentum_fade_detected`` threshold.
    strong_body_threshold:
        Body ratio above which a candle counts as strongly directional.
        Default 0.60 matches ``is_strong_candle``.
    adverse_wick_threshold:
        Informational threshold; captured in ``FadeAnalysis`` for logging but
        not used in the *fade_detected* computation.
    confirmation_bars:
        Minimum number of weak candles in the window required to confirm a
        fade.  Default 1 reproduces Phase 4 behavior.  Set to 2 for SCALP to
        require at least two weak bars before tightening (avoids reacting to a
        single doji inside a strong move).

    Returns
    -------
    FadeAnalysis
        Always returns a valid struct.  All metrics are zero / False when the
        window is disabled (``window < 1``) or there are insufficient candles
        (``len(candles) < window + 1``).
    """
    if window < 1 or len(candles) < window + 1:
        return _FADE_EMPTY

    dir_upper = direction.upper()
    eval_window: list[Candle] = list(candles[-window:])

    body_ratios = [body_to_range_ratio(o, h, l, c) for o, h, l, c in eval_window]
    wick_ratios_adv = [wick_ratio(o, h, l, c, dir_upper) for o, h, l, c in eval_window]

    weak_count = sum(1 for r in body_ratios if r < weak_body_threshold)
    confirm_met = weak_count >= confirmation_bars
    shrinking = shrinking_body_sequence(candles, n=window)

    last_o, last_h, last_l, last_c = eval_window[-1]
    last_body = body_ratios[-1]
    last_wick = wick_ratios_adv[-1]
    last_strong = is_strong_candle(last_o, last_h, last_l, last_c, dir_upper, strong_body_threshold)
    consec = consecutive_strong_count(candles, dir_upper, strong_body_threshold)

    return FadeAnalysis(
        body_ratios=body_ratios,
        wick_ratios_adverse=wick_ratios_adv,
        consecutive_strong_candles=consec,
        shrinking_body_sequence=shrinking,
        weak_candles_in_window=weak_count,
        confirmation_bars_met=confirm_met,
        fade_detected=shrinking and confirm_met,
        last_body_ratio=last_body,
        last_wick_ratio_adverse=last_wick,
        last_is_strong=last_strong,
    )
