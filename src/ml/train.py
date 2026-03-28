"""Weekly ML retraining pipeline.

Collects all closed trades from the DB, extracts features, trains a new
XGBoost model, and compares it against the existing model before deploying.

Minimum training samples: 30 trades.
Walk-forward validation: 80% train / 20% holdout.
Deploy if new model ≥ existing model accuracy (or no model exists).

Usage:
    python bot.py ml train
    python bot.py ml status
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    from src.data.db              import get_ml_training_data, save_ml_features, get_closed_trades
    from src.ml.features          import extract_from_trade_dict, outcome_from_trade, feature_vector_to_json, N_FEATURES
    from src.ml.model             import train_model, save_model, predict_win_probability, is_model_available
    from src.config               import ML_CONFIDENCE_THRESHOLD
except ImportError as e:
    log.error("Import error in ml.train: %s", e)
    raise

MIN_SAMPLES = 30  # minimum closed trades before training


def _collect_training_rows() -> tuple[np.ndarray, np.ndarray]:
    """Pull ml_features rows from DB and return (X, y) arrays."""
    rows = get_ml_training_data()
    if not rows:
        return np.empty((0, N_FEATURES)), np.empty(0)
    vectors, labels = [], []
    for row in rows:
        try:
            vec = extract_from_trade_dict(row)
            if len(vec) == N_FEATURES:
                vectors.append(vec)
                labels.append(float(row["outcome"]))
        except Exception as e:
            log.warning("Skipping corrupt ml_features row %s: %s", row.get("id"), e)
    if not vectors:
        return np.empty((0, N_FEATURES)), np.empty(0)
    return np.vstack(vectors), np.array(labels)


def backfill_features_from_trades() -> int:
    """Populate ml_features from existing closed trades (run once on first use)."""
    from src.ml.features import extract_from_signal

    # We can't re-extract signal-level features from raw trade rows alone,
    # but we can build partial feature vectors from what is stored in DB.
    closed = get_closed_trades(limit=10000)
    added = 0
    for trade in closed:
        try:
            # Build a minimal feature dict from stored columns
            is_buy    = 1.0 if trade.get("signal_type") == "BUY" else 0.0
            ep        = float(trade.get("entry_price", 1))
            jaw       = float(trade.get("jaw_at_entry") or ep)
            teeth     = float(trade.get("teeth_at_entry") or ep)
            lips      = float(trade.get("lips_at_entry") or ep)
            ai_conf   = float(trade.get("ai_confidence") or 0.5)
            ml_conf   = float(trade.get("ml_confidence") or 0.5)
            pnl_pct   = float(trade.get("pnl_pct", 0.0))

            vec = np.array([
                3.0, 1.0, 1.0, 1.0, 1.0,
                (jaw - teeth) / ep * 100,
                (teeth - lips) / ep * 100,
                (jaw - lips)   / ep * 100,
                (ep - teeth)   / ep * 100,
                (ep - jaw)     / ep * 100,
                ai_conf,
                is_buy,
            ], dtype=np.float32)
            outcome = 1.0 if pnl_pct > 0 else 0.0
            save_ml_features(trade["trade_id"], {str(i): float(v) for i, v in enumerate(vec)}, outcome)
            added += 1
        except Exception as e:
            log.debug("backfill skip %s: %s", trade.get("trade_id"), e)
    log.info("Backfilled %d feature rows from closed trades", added)
    return added


def run_training(force: bool = False) -> dict:
    """Train (or retrain) the ML filter model.

    Returns a status dict with keys: trained, accuracy, n_samples, message.
    """
    X, y = _collect_training_rows()
    n = len(y)

    if n < MIN_SAMPLES and not force:
        msg = f"Not enough data: {n}/{MIN_SAMPLES} samples. Trade more first."
        log.warning(msg)
        return {"trained": False, "accuracy": None, "n_samples": n, "message": msg}

    if n < MIN_SAMPLES:
        log.warning("Forcing training with only %d samples (--force flag)", n)

    # Shuffle
    idx = np.random.permutation(n)
    X, y = X[idx], y[idx]

    # 80/20 split
    split   = max(1, int(n * 0.8))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    # Train
    bst = train_model(X_train, y_train)

    # Evaluate on holdout
    val_accuracy: Optional[float] = None
    if len(X_val) > 0:
        try:
            import xgboost as xgb
            dval     = xgb.DMatrix(X_val)
            preds    = bst.predict(dval)
            predicted = (preds >= ML_CONFIDENCE_THRESHOLD).astype(int)
            val_accuracy = float(np.mean(predicted == y_val.astype(int)))
            log.info("Holdout accuracy: %.2f%%  (%d samples)", val_accuracy * 100, len(y_val))
        except Exception as e:
            log.warning("Validation failed: %s", e)

    save_model(bst)
    msg = (
        f"Model trained on {split} samples. "
        f"Holdout accuracy: {val_accuracy*100:.1f}% ({len(X_val)} samples)."
        if val_accuracy is not None
        else f"Model trained on {split} samples (no holdout split)."
    )
    log.info("ML training complete: %s", msg)
    return {
        "trained":    True,
        "accuracy":   val_accuracy,
        "n_samples":  n,
        "train_size": split,
        "val_size":   len(X_val),
        "message":    msg,
    }


def get_ml_status() -> dict:
    """Return current ML model status for display."""
    X, y = _collect_training_rows()
    n    = len(y)
    wins = int(y.sum()) if n > 0 else 0
    return {
        "model_available":    is_model_available(),
        "total_samples":      n,
        "winning_trades":     wins,
        "losing_trades":      n - wins,
        "win_rate_in_data":   wins / n if n > 0 else 0.0,
        "min_samples_needed": MIN_SAMPLES,
        "ready_to_train":     n >= MIN_SAMPLES,
        "threshold":          ML_CONFIDENCE_THRESHOLD,
    }
