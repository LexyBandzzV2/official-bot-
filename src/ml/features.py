"""Feature extraction for ML model training and inference.

This module defines the *canonical* ML feature contract for the bot.

Key invariant:
  - The exact feature order in ``FEATURE_NAMES`` is used everywhere:
      signal -> DB (ml_features.features_json) -> training -> inference

We intentionally keep this contract stable and versioned.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

log = logging.getLogger(__name__)

FEATURE_VERSION = 2

# ── Feature names (must stay in this exact order for model compatibility) ──────
# Uses Heikin Ashi values to avoid mixing candle types.
FEATURE_NAMES = [
    # Core candle features (HA)
    "ha_open",
    "ha_high",
    "ha_low",
    "ha_close",
    "volume",

    # Candle metrics
    "candle_range",
    "candle_body",

    # Volatility (std of returns)
    "volatility_10",
    "volatility_20",

    # ATR (HA-based)
    "atr_14",

    # Indicators at signal bar
    "vi_plus",
    "vi_minus",
    "stoch_k",
    "stoch_d",
    "jaw",
    "teeth",
    "lips",

    # Time features (UTC)
    "hour_of_day",
    "day_of_week",

    # Signal context
    "points",              # 0-3
    "alligator_point",     # 0/1
    "stochastic_point",    # 0/1
    "vortex_point",        # 0/1
    "ai_confidence",       # 0..1 (0.5 default)
    "is_buy",              # 1 BUY, 0 SELL
]

N_FEATURES = len(FEATURE_NAMES)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


def extract_from_signal_and_frame(sig: Any, frame_df) -> np.ndarray:
    """Build feature vector from a signal and the latest enriched candle frame.

    ``frame_df`` must include (at least) the last row with:
      ha_open/ha_high/ha_low/ha_close/volume, jaw/teeth/lips, vi_plus/vi_minus, stoch_k/stoch_d, and time.
    """
    if frame_df is None or getattr(frame_df, "empty", True):
        # Fallback: produce a safe zero vector with signal context only.
        is_buy = 1.0 if getattr(sig, "signal_type", "") == "BUY" else 0.0
        vec = np.zeros(N_FEATURES, dtype=np.float32)
        vec[FEATURE_NAMES.index("points")] = _safe_float(getattr(sig, "points", 0.0))
        vec[FEATURE_NAMES.index("alligator_point")] = _safe_float(getattr(sig, "alligator_point", 0.0))
        vec[FEATURE_NAMES.index("stochastic_point")] = _safe_float(getattr(sig, "stochastic_point", 0.0))
        vec[FEATURE_NAMES.index("vortex_point")] = _safe_float(getattr(sig, "vortex_point", 0.0))
        vec[FEATURE_NAMES.index("ai_confidence")] = _safe_float(getattr(sig, "ai_confidence", None), 0.5)
        vec[FEATURE_NAMES.index("is_buy")] = _safe_float(is_buy)
        return vec

    last = frame_df.iloc[-1]
    ha_open = _safe_float(last.get("ha_open"))
    ha_high = _safe_float(last.get("ha_high"))
    ha_low = _safe_float(last.get("ha_low"))
    ha_close = _safe_float(last.get("ha_close"))
    volume = _safe_float(last.get("volume"))

    candle_range = _safe_float(ha_high - ha_low)
    candle_body = _safe_float(abs(ha_close - ha_open))

    # volatility features expect to already exist if caller computed them
    vol10 = _safe_float(last.get("volatility_10"))
    vol20 = _safe_float(last.get("volatility_20"))
    atr14 = _safe_float(last.get("atr_14"))

    vi_plus = _safe_float(last.get("vi_plus"))
    vi_minus = _safe_float(last.get("vi_minus"))
    stoch_k = _safe_float(last.get("stoch_k"))
    stoch_d = _safe_float(last.get("stoch_d"))
    jaw = _safe_float(last.get("jaw"))
    teeth = _safe_float(last.get("teeth"))
    lips = _safe_float(last.get("lips"))

    # time features (UTC)
    hour_of_day = 0.0
    day_of_week = 0.0
    t = last.get("time")
    try:
        if t is not None:
            ts = t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
            if isinstance(ts, datetime):
                hour_of_day = float(ts.hour)
                day_of_week = float(ts.weekday())
    except Exception:
        pass

    is_buy = 1.0 if getattr(sig, "signal_type", "") == "BUY" else 0.0
    vec = np.array(
        [
            ha_open, ha_high, ha_low, ha_close, volume,
            candle_range, candle_body,
            vol10, vol20,
            atr14,
            vi_plus, vi_minus, stoch_k, stoch_d,
            jaw, teeth, lips,
            hour_of_day, day_of_week,
            _safe_float(getattr(sig, "points", 0.0)),
            _safe_float(getattr(sig, "alligator_point", 0.0)),
            _safe_float(getattr(sig, "stochastic_point", 0.0)),
            _safe_float(getattr(sig, "vortex_point", 0.0)),
            _safe_float(getattr(sig, "ai_confidence", None), 0.5),
            is_buy,
        ],
        dtype=np.float32,
    )
    # final correctness guard
    vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return vec


def extract_from_trade_dict(row: dict) -> np.ndarray:
    """Reconstruct feature vector from stored ml_features table row.

    The row must have a 'features_json' column containing the serialised vector.
    """
    raw = json.loads(row["features_json"])
    # Accept both list-vector (new) and {"0": v0, ...} (legacy)
    if isinstance(raw, list):
        vec = np.array(raw, dtype=np.float32)
    elif isinstance(raw, dict):
        try:
            items = sorted(((int(k), float(v)) for k, v in raw.items()), key=lambda x: x[0])
            vec = np.array([v for _, v in items], dtype=np.float32)
        except Exception:
            vec = np.array([], dtype=np.float32)
    else:
        vec = np.array([], dtype=np.float32)

    vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return vec


def feature_vector_to_json(vec: np.ndarray) -> str:
    """Serialise feature vector to JSON string for DB storage."""
    return json.dumps(vec.tolist())


def outcome_from_trade(trade: dict) -> float:
    """Derive binary outcome label from a closed trade row.

    1.0 = winner (pnl_pct > 0), 0.0 = loser.
    """
    pnl_pct = float(trade.get("pnl_pct", 0.0))
    return 1.0 if pnl_pct > 0 else 0.0
