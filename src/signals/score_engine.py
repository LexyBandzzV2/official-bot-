"""Signal score engine — Phase 5 + Strategy Alignment.

Computes a transparent 0–100 numeric score for every evaluated signal,
broken down into per-component sub-scores so you can see *exactly* why a
signal was accepted or rejected.

Score components
----------------
structure_points          (max 20)
    Alligator in correct alignment (lips above teeth+jaw for BUY, below for SELL).
    20 if alligator_point, else 0.

indicator_points          (max 20)
    10 × (stochastic_point + vortex_point).
    20 = both fired, 10 = one fired, 0 = neither.

timeframe_alignment_points  (max 10)
    10 for formal timeframes (3m / 5m / 15m / 1h / 2h / 4h).
    5  for informal timeframes (1m, 30m, 3h, 1d …).
    Formal set is the same frozenset defined in Phase 1 / 4.

candle_quality_points       (max 20)
    Derived from the last Heikin-Ashi bar's body-to-range ratio using the
    candle_quality module introduced in Phase 4.
    ≥ 0.70 → 20 (strong), ≥ 0.40 → 10 (moderate), < 0.40 → 0 (weak / indecisive).
    Graceful: returns 0 when *ha_df* is None or the candle_quality import fails.

volatility_points           (max 10)
    10 when the most-recent ATR (column ``atr_14``) is present and > 0.
    0  when ha_df is None or the column is missing.

market_structure_points     (max 15) ← Strategy Point 1
    Detects Higher Highs / Higher Lows (BUY) or Lower Highs / Lower Lows (SELL)
    in the last 30 bars using swing-pivot analysis.
    15 = clear HH+HL (or LH+LL), 7 = partial structure, 0 = no structure / chop.
    Prevents entries in chop zones where indicator confluence fires on noise.

rr_adjustment_points        (-20 to +8) ← Entry Rule: "R:R must be acceptable"
    Compares estimated profit target (2.5× ATR) against configured stop-loss %.
    R:R ≥ 2.5 → +8, ≥ 1.5 → 0, ≥ 1.0 → -10, < 1.0 → -20 (near-fatal to score).
    Implements the strategy's explicit R:R requirement before any trade is taken.

overextension_points        (-15 to 0) ← Strategy: "Avoid late entries"
    Penalises entries where price has already moved too far from the Alligator lips.
    > 2.5× ATR from lips → -15, > 1.5× ATR → -7, ≤ 1.5× ATR → 0.
    Prevents chasing overextended moves that are likely to retrace.

ml_adjustment_points        (max +10 / min -20)
    Applied *after* the ML gate fires:
      • ml_prob ≥ threshold + 0.15  → +10 ("boosted")
      • threshold ≤ ml_prob < threshold + 0.15 →  0 ("passed")
      • ml_prob < threshold              → -20 ("vetoed")
    Symmetrically applied for AI via apply_ai_effect().

score_total  =  structure + indicator + tf_alignment + candle_quality
                + volatility + market_structure + rr_adjustment
                + overextension + ml_adjustment_points

Maximum possible score ≈ 20+20+10+20+10+15+8 = 103 (before ML/AI).

Usage
-----
Call *compute_score* immediately after evaluate_ha() to fill sub-scores.
Call *apply_ml_effect* after the ML gate resolves.
Call *apply_ai_effect* after the AI gate resolves.
Both mutate the signal object in-place; they are safe to call even if
compute_score was never called (they guard against AttributeError).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Boost threshold: ml_prob > threshold + BOOST_MARGIN qualifies as "boosted"
_BOOST_MARGIN: float = 0.15
_BOOST_SCORE:  float = 10.0
_VETO_SCORE:   float = -20.0

# candle_quality thresholds for point bands
_CANDLE_STRONG:   float = 0.70   # body_ratio \u2265 0.70 \u2192 20 pts
_CANDLE_MODERATE: float = 0.40   # body_ratio \u2265 0.40 \u2192 10 pts; < 0.40 \u2192 0 pts


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(sig: Any, attr: str, default=None):
    return getattr(sig, attr, default)


def _tf_points(timeframe: str) -> float:
    try:
        from src.signals.strategy_mode import is_formal_timeframe
        return 10.0 if is_formal_timeframe(timeframe) else 5.0
    except Exception:
        return 5.0


def _candle_quality_points(ha_df: Any) -> float:
    """Return 0 / 10 / 20 based on the last bar's body-to-range ratio."""
    if ha_df is None:
        return 0.0
    try:
        from src.risk.candle_quality import body_to_range_ratio
        last = ha_df.iloc[-1]
        o = float(last.get("ha_open",  last.get("open",  0.0)))
        h = float(last.get("ha_high",  last.get("high",  0.0)))
        l = float(last.get("ha_low",   last.get("low",   0.0)))
        c = float(last.get("ha_close", last.get("close", 0.0)))
        ratio = body_to_range_ratio(o, h, l, c)
        if ratio >= _CANDLE_STRONG:
            return 20.0
        if ratio >= _CANDLE_MODERATE:
            return 10.0
        return 0.0
    except Exception as exc:
        log.debug("candle_quality_points: %s", exc)
        return 0.0


def _volatility_points(ha_df: Any) -> float:
    """Return 10 when the last ATR value is usable, else 0."""
    if ha_df is None:
        return 0.0
    try:
        import numpy as np
        if "atr_14" not in ha_df.columns:
            return 0.0
        atr_val = ha_df["atr_14"].iloc[-1]
        if atr_val is None or (hasattr(atr_val, "__float__") and np.isnan(float(atr_val))):
            return 0.0
        return 10.0 if float(atr_val) > 0.0 else 0.0
    except Exception as exc:
        log.debug("volatility_points: %s", exc)
        return 0.0


def _market_structure_points(ha_df: Any, direction: str) -> float:
    """Return 0 / 7 / 15 based on swing-pivot market structure.

    Strategy Point 1: Higher Highs + Higher Lows for longs;
                      Lower Highs + Lower Lows for shorts.

    Uses HA highs/lows to detect swing pivots in the last 30 bars.
    A pivot high is a bar whose high is the highest in a ±2 bar window.
    Requires at least 2 pivot highs and 2 pivot lows to compare.

    Returns
    -------
    15.0  — both conditions met (full HH+HL or LH+LL)
     7.0  — one condition met (partial structure)
     0.0  — neither met, or insufficient data (chop / no trend)
    """
    if ha_df is None or len(ha_df) < 20:
        return 0.0
    try:
        import numpy as np
        h_col = "ha_high"  if "ha_high"  in ha_df.columns else "high"
        l_col = "ha_low"   if "ha_low"   in ha_df.columns else "low"

        highs = ha_df[h_col].values.astype(float)
        lows  = ha_df[l_col].values.astype(float)
        n     = len(highs)
        window = 2   # bars each side for pivot detection

        pivot_highs: list[float] = []
        pivot_lows:  list[float] = []

        # Scan last 30 bars for pivot points (exclude last 2 — not yet confirmed)
        start = max(window, n - 30)
        for i in range(start, n - window):
            if highs[i] == np.max(highs[i - window: i + window + 1]):
                pivot_highs.append(highs[i])
            if lows[i] == np.min(lows[i - window: i + window + 1]):
                pivot_lows.append(lows[i])

        if len(pivot_highs) < 2 or len(pivot_lows) < 2:
            return 0.0

        is_buy = direction.upper() == "BUY"
        if is_buy:
            higher_high = pivot_highs[-1] > pivot_highs[-2]   # last pivot high > previous
            higher_low  = pivot_lows[-1]  > pivot_lows[-2]    # last pivot low > previous
            score = int(higher_high) + int(higher_low)
        else:
            lower_high = pivot_highs[-1] < pivot_highs[-2]
            lower_low  = pivot_lows[-1]  < pivot_lows[-2]
            score = int(lower_high) + int(lower_low)

        return {2: 15.0, 1: 7.0, 0: 0.0}[score]
    except Exception as exc:
        log.debug("market_structure_points: %s", exc)
        return 0.0


def _overextension_points(ha_df: Any, direction: str) -> float:
    """Return 0 / -7 / -15 penalty when price is too far from Alligator lips.

    Strategy: "Avoid late entries after overextension."

    If price has already run 1.5–2.5× ATR past the lips, the move is likely
    overextended and the risk of entering now is buying/selling the extreme.

    Returns
    -------
     0.0  — within normal range (≤ 1.5× ATR from lips)
    -7.0  — moderately overextended (1.5–2.5× ATR)
   -15.0  — severely overextended (> 2.5× ATR)
    """
    if ha_df is None or len(ha_df) < 5:
        return 0.0
    try:
        import numpy as np
        last = ha_df.iloc[-1]
        close = float(last.get("ha_close", last.get("close", 0.0)))
        lips  = float(last.get("lips",  0.0))

        if "atr_14" not in ha_df.columns:
            return 0.0
        atr = float(ha_df["atr_14"].iloc[-1])

        if close <= 0 or lips <= 0 or atr <= 0 or np.isnan(atr):
            return 0.0

        dist = abs(close - lips)
        is_buy = direction.upper() == "BUY"

        # Only penalise when price is ahead of lips in the trade direction
        # (i.e. above lips for longs — lips are the fastest Alligator line)
        if (is_buy and close > lips) or (not is_buy and close < lips):
            if dist > 2.5 * atr:
                return -15.0
            if dist > 1.5 * atr:
                return -7.0
        return 0.0
    except Exception as exc:
        log.debug("overextension_points: %s", exc)
        return 0.0


def _rr_adjustment_points(sig: Any) -> float:
    """Return -20 / -10 / 0 / +8 based on estimated Risk:Reward.

    Entry rules require "Risk-to-reward is acceptable."

    Target is estimated as 2.5× ATR from entry — a realistic first profit target
    for high-volatility assets.  Stop is the configured hard stop-loss %.

    R:R = (2.5 × ATR%) / stop_loss%

    Returns
    -------
    +8.0   — R:R ≥ 2.5 (excellent)
     0.0   — 1.5 ≤ R:R < 2.5 (acceptable)
   -10.0   — 1.0 ≤ R:R < 1.5 (marginal — likely scratch trade)
   -20.0   — R:R < 1.0 (stop wider than realistic profit — avoid)
    """
    try:
        atr_pct = float(getattr(sig, "prefilter_atr_pct", 0.0))
        if atr_pct <= 0:
            return 0.0   # no ATR data — fail open

        target_pct = 2.5 * atr_pct   # first realistic profit target = 2.5× ATR

        try:
            from src.config import STOP_LOSS_PCT
            stop_pct = float(STOP_LOSS_PCT) * 100.0   # convert 0.02 → 2.0%
        except Exception:
            stop_pct = 2.0

        if stop_pct <= 0:
            return 0.0

        rr = target_pct / stop_pct
        if rr >= 2.5:
            return 8.0
        if rr >= 1.5:
            return 0.0
        if rr >= 1.0:
            return -10.0
        return -20.0
    except Exception as exc:
        log.debug("rr_adjustment_points: %s", exc)
        return 0.0


# ── Primary API ───────────────────────────────────────────────────────────────

def compute_score(sig: Any, ha_df: Any = None) -> None:
    """Populate score sub-fields on *sig* in-place.

    Parameters
    ----------
    sig:
        A ``BuySignalResult`` or ``SellSignalResult`` instance.
    ha_df:
        Optional Heikin-Ashi DataFrame for the current symbol (used for
        candle-quality and ATR-volatility scoring).  Pass ``None`` to skip
        those two components gracefully.

    Sets
    ----
    sig.structure_points, sig.indicator_points,
    sig.timeframe_alignment_points, sig.candle_quality_points,
    sig.volatility_points, sig.market_structure_points,
    sig.rr_adjustment_points, sig.overextension_points, sig.score_total.

    ``ml_adjustment_points`` is NOT set here; call *apply_ml_effect* /
    *apply_ai_effect* after the respective gates resolve.
    """
    al  = bool(_get(sig, "alligator_point",  False))
    st  = bool(_get(sig, "stochastic_point", False))
    vo  = bool(_get(sig, "vortex_point",     False))
    tf  = _get(sig, "timeframe", "")
    direction = str(_get(sig, "signal_type", "BUY"))

    sig.structure_points           = 20.0 if al else 0.0
    sig.indicator_points           = 10.0 * (int(st) + int(vo))
    sig.timeframe_alignment_points = _tf_points(tf)
    sig.candle_quality_points      = _candle_quality_points(ha_df)
    sig.volatility_points          = _volatility_points(ha_df)
    # Strategy alignment components
    sig.market_structure_points    = _market_structure_points(ha_df, direction)
    sig.rr_adjustment_points       = _rr_adjustment_points(sig)
    sig.overextension_points       = _overextension_points(ha_df, direction)

    sig.score_total = (
        sig.structure_points
        + sig.indicator_points
        + sig.timeframe_alignment_points
        + sig.candle_quality_points
        + sig.volatility_points
        + sig.market_structure_points
        + sig.rr_adjustment_points
        + sig.overextension_points
    )
    # ml_adjustment_points stays at 0 until apply_ml_effect is called


def apply_ml_effect(sig: Any, ml_prob: float, threshold: float) -> None:
    """Apply the ML gate result to *sig*'s score and set sig.ml_effect.

    Must be called *after* compute_score().

    Parameters
    ----------
    sig:
        Signal result object.
    ml_prob:
        ML model probability for this signal (0.0–1.0).
    threshold:
        Configured threshold from ``ML_CONFIDENCE_THRESHOLD``.

    Side-effects
    ------------
    Sets ``sig.ml_effect``, ``sig.ml_adjustment_points``, updates
    ``sig.score_total``.
    """
    if ml_prob >= threshold + _BOOST_MARGIN:
        effect, adj = "boosted", _BOOST_SCORE
    elif ml_prob >= threshold:
        effect, adj = "passed", 0.0
    else:
        effect, adj = "vetoed", _VETO_SCORE

    # Remove any previous ML adjustment from score_total before re-applying
    old_adj = float(_get(sig, "ml_adjustment_points", 0.0))
    sig.score_total = float(_get(sig, "score_total", 0.0)) - old_adj + adj

    sig.ml_effect            = effect
    sig.ml_adjustment_points = adj


def apply_ai_effect(sig: Any, ai_score: float, threshold: float) -> None:
    """Apply the AI gate result to *sig*'s score and set sig.ai_effect.

    Uses a separate adjustment field stored multiplied into score_total.
    The AI adjustment is tracked separately from ``ml_adjustment_points``
    so downstream analytics can attribute each contribution independently.

    Parameters
    ----------
    sig:
        Signal result object.
    ai_score:
        AI confidence score (0.0–1.0).
    threshold:
        Configured threshold from ``AI_CONFIDENCE_THRESHOLD``.
    """
    if ai_score >= threshold + _BOOST_MARGIN:
        effect, adj = "boosted", _BOOST_SCORE
    elif ai_score >= threshold:
        effect, adj = "passed", 0.0
    else:
        effect, adj = "vetoed", _VETO_SCORE

    # Track AI adjustment separately — stored in ai_adjustment_points if present
    # (sig may not have that field; we absorb into score_total directly and
    # store the effect string so analytics can recover it)
    sig.score_total = float(_get(sig, "score_total", 0.0)) + adj
    sig.ai_effect   = effect
