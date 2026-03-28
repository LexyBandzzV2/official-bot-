"""XGBoost false-signal filter model.

Role: predict the probability that a given signal leads to a profitable trade.
Signals with win probability below ML_CONFIDENCE_THRESHOLD (default 0.60) are
suppressed before execution.

The model is saved/loaded from models/signal_filter.json.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import xgboost as xgb
    _XGB_AVAILABLE = True
except ImportError:
    _XGB_AVAILABLE = False
    log.warning("xgboost not installed — ML filtering disabled")

try:
    from src.config import MODELS_DIR, ML_CONFIDENCE_THRESHOLD
    from src.ml.features import extract_from_signal, N_FEATURES
except ImportError:
    MODELS_DIR              = Path("models")
    ML_CONFIDENCE_THRESHOLD = 0.60
    from src.ml.features import extract_from_signal, N_FEATURES

_MODEL_PATH = Path(MODELS_DIR) / "signal_filter.json"
_model: Optional[Any] = None   # xgb.Booster or None


def _get_model() -> Optional[Any]:
    global _model
    if _model is not None:
        return _model
    if not _XGB_AVAILABLE:
        return None
    if _MODEL_PATH.exists():
        try:
            bst = xgb.Booster()
            bst.load_model(str(_MODEL_PATH))
            _model = bst
            log.info("ML model loaded from %s", _MODEL_PATH)
        except Exception as e:
            log.warning("Failed to load ML model: %s", e)
    return _model


def is_model_available() -> bool:
    """Return True if a trained model exists and XGBoost is installed."""
    return _XGB_AVAILABLE and _MODEL_PATH.exists()


def predict_win_probability(sig: Any) -> Optional[float]:
    """Predict the probability (0.0–1.0) that the signal trades profitably.

    Returns None if model is not trained yet (new bot, not enough data).
    """
    bst = _get_model()
    if bst is None:
        return None
    try:
        vec  = extract_from_signal(sig).reshape(1, -1)
        dmat = xgb.DMatrix(vec, feature_names=None)
        prob = float(bst.predict(dmat)[0])
        return max(0.0, min(1.0, prob))
    except Exception as e:
        log.error("ML predict failed: %s", e)
        return None


def passes_ml_filter(sig: Any) -> tuple[bool, Optional[float]]:
    """Check if a signal passes the ML confidence threshold.

    Returns (passed: bool, confidence: Optional[float]).
    If the model is not available, passes by default (no false rejection).
    """
    prob = predict_win_probability(sig)
    if prob is None:
        return True, None   # model not trained yet — allow through
    passed = prob >= ML_CONFIDENCE_THRESHOLD
    return passed, prob


def save_model(booster: Any) -> None:
    """Persist a newly trained XGBoost booster."""
    global _model
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    booster.save_model(str(_MODEL_PATH))
    _model = booster
    log.info("ML model saved to %s", _MODEL_PATH)


def train_model(
    X: np.ndarray,
    y: np.ndarray,
    n_rounds: int = 200,
    params: Optional[dict] = None,
) -> Any:
    """Train a fresh XGBoost binary classifier.

    Args:
        X:        Feature matrix, shape (n_samples, N_FEATURES)
        y:        Binary labels  (1 = winner, 0 = loser)
        n_rounds: Number of boosting rounds
        params:   XGBoost params (defaults used if None)

    Returns:
        Trained xgb.Booster instance.
    """
    if not _XGB_AVAILABLE:
        raise RuntimeError("xgboost is not installed — run: pip install xgboost")

    default_params = {
        "objective":       "binary:logistic",
        "eval_metric":     "logloss",
        "eta":             0.05,
        "max_depth":       4,
        "subsample":       0.8,
        "colsample_bytree":0.8,
        "min_child_weight":3,
        "seed":            42,
        "verbosity":       0,
    }
    if params:
        default_params.update(params)

    dtrain = xgb.DMatrix(X, label=y)
    booster = xgb.train(
        default_params,
        dtrain,
        num_boost_round=n_rounds,
        verbose_eval=False,
    )
    return booster
