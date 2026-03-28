"""Feature extraction for ML model training and inference.

Each closed trade is converted to a numeric feature vector for XGBoost.
Features capture signal quality, market structure, and entry context.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# ── Feature names (must stay in this exact order for model compatibility) ──────
FEATURE_NAMES = [
    # Signal breadth
    "points",                # 0–3: how many indicators confirmed
    "alligator_point",       # 0 or 1
    "stochastic_point",      # 0 or 1
    "vortex_point",          # 0 or 1
    # "staircase_confirmed" removed

    # Alligator spread at entry (normalised by entry price)
    "jaw_teeth_spread_pct",   # (jaw - teeth) / entry * 100
    "teeth_lips_spread_pct",  # (teeth - lips) / entry * 100
    "jaw_lips_spread_pct",    # (jaw - lips) / entry * 100

    # Price context
    "entry_vs_teeth_pct",     # (entry - teeth) / entry * 100
    "entry_vs_jaw_pct",       # (entry - jaw) / entry * 100

    # AI score
    "ai_confidence",          # 0.0–1.0 (or 0.5 if unavailable)

    # Signal type
    "is_buy",                 # 1 = BUY, 0 = SELL
]

N_FEATURES = len(FEATURE_NAMES)


def extract_from_signal(sig: Any) -> np.ndarray:
    """Build feature vector from a BuySignalResult or SellSignalResult.

    Returns float32 array of shape (N_FEATURES,).
    """
    ep = sig.entry_price if sig.entry_price else 1.0
    is_buy = 1.0 if sig.signal_type == "BUY" else 0.0

    jaw    = sig.jaw_price or ep
    teeth  = sig.teeth_price or ep
    lips   = sig.lips_price or ep

    vec = [
        float(sig.points),
        float(sig.alligator_point),
        float(sig.stochastic_point),
        float(sig.vortex_point),
        # staircase_confirmed removed
        (jaw - teeth) / ep * 100,
        (teeth - lips) / ep * 100,
        (jaw - lips)   / ep * 100,
        (ep - teeth)   / ep * 100,
        (ep - jaw)     / ep * 100,
        float(sig.ai_confidence) if sig.ai_confidence is not None else 0.5,
        is_buy,
    ]
    return np.array(vec, dtype=np.float32)


def extract_from_trade_dict(row: dict) -> np.ndarray:
    """Reconstruct feature vector from stored ml_features table row.

    The row must have a 'features_json' column containing the serialised vector.
    """
    return np.array(json.loads(row["features_json"]), dtype=np.float32)


def feature_vector_to_json(vec: np.ndarray) -> str:
    """Serialise feature vector to JSON string for DB storage."""
    return json.dumps(vec.tolist())


def outcome_from_trade(trade: dict) -> float:
    """Derive binary outcome label from a closed trade row.

    1.0 = winner (pnl_pct > 0), 0.0 = loser.
    """
    pnl_pct = float(trade.get("pnl_pct", 0.0))
    return 1.0 if pnl_pct > 0 else 0.0
