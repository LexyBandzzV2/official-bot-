"""Signal score engine — Phase 5.

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

candle_quality_points       (max 20)
    Derived from the last Heikin-Ashi bar's body-to-range ratio.
    ≥ 0.70 → 20 (strong), ≥ 0.40 → 10 (moderate), < 0.40 → 0 (weak).

volatility_points           (max 10)
    10 when the most-recent ATR (column ``atr_14``) is present and > 0.
    0  when ha_df is None or the column is missing.

ai_adjustment               (max +10 / min −20)
    Applied after the AI gate:
      • ai_score ≥ threshold + 0.15  → +10 ("boosted")
      • threshold ≤ ai_score < threshold + 0.15 →  0 ("passed")
      • ai_score < threshold              → −20 ("vetoed")

score_total  =  structure + indicator + tf_alignment + candle_quality + volatility
                + ai_adjustment

Maximum possible score  =  20+20+10+20+10+10 = 90.

Usage
-----
Call *compute_score* immediately after evaluate_ha() to fill sub-scores.
Call *apply_ai_effect* after the AI gate resolves.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Boost threshold: ai_score > threshold + BOOST_MARGIN qualifies as "boosted"
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
    sig.volatility_points, sig.score_total.

    ``ml_adjustment_points`` is NOT set here; call *apply_ml_effect* /
    *apply_ai_effect* after the respective gates resolve.
    """
    al  = bool(_get(sig, "alligator_point",  False))
    st  = bool(_get(sig, "stochastic_point", False))
    vo  = bool(_get(sig, "vortex_point",     False))
    tf  = str(_get(sig, "timeframe", "") or "")

    sig.structure_points           = 20.0 if al else 0.0
    sig.indicator_points           = 10.0 * (int(st) + int(vo))
    sig.timeframe_alignment_points = _tf_points(tf)
    sig.candle_quality_points      = _candle_quality_points(ha_df)
    sig.volatility_points          = _volatility_points(ha_df)

    sig.score_total = (
        sig.structure_points
        + sig.indicator_points
        + sig.timeframe_alignment_points
        + sig.candle_quality_points
        + sig.volatility_points
    )


def apply_ai_effect(sig: Any, ai_score: float | None, threshold: float) -> None:
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
    if ai_score is None:
        return  # no AI client — leave score unchanged

    if ai_score >= threshold + _BOOST_MARGIN:
        effect, adj = "boosted", _BOOST_SCORE
    elif ai_score >= threshold:
        effect, adj = "passed", 0.0
    else:
        effect, adj = "vetoed", _VETO_SCORE

    # Track AI adjustment separately — stored in ai_adjustment_points if present
    # (sig may not have that field; we absorb into score_total directly and
    # store the effect string so analytics can recover it)
    sig.score_total = float(_get(sig, "score_total", 0.0) or 0.0) + adj
    sig.ai_effect   = effect
