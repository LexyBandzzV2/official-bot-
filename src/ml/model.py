"""Local ML false-signal filter model (XGBoost + LightGBM ensemble).

Role:
  - predict P(win) for a candidate signal using only local models
  - use it as a *filter*, not a market oracle

Correctness properties:
  - outputs are always clamped to [0.0, 1.0]
  - on any model/scaler load or predict error, returns None (never blocks trading)
  - latency + error metrics are recorded to DB periodically (ml_model_health)

Artifacts (MODELS_DIR):
  - xgboost_model.json
  - lightgbm_model.txt
  - ml_scaler.joblib
  - ml_metadata.json
"""

from __future__ import annotations

import logging
import time
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
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    _LGB_AVAILABLE = False
    log.warning("lightgbm not installed — LightGBM disabled")

try:
    import joblib
    _JOBLIB_AVAILABLE = True
except ImportError:
    _JOBLIB_AVAILABLE = False
    log.warning("joblib not installed — scaler disabled")

try:
    from src.config import MODELS_DIR, ML_CONFIDENCE_THRESHOLD
    from src.ml.features import extract_from_signal_and_frame, N_FEATURES
    from src.data.db import save_ml_model_health
except ImportError:
    MODELS_DIR              = Path("models")
    ML_CONFIDENCE_THRESHOLD = 0.60
    from src.ml.features import extract_from_signal_and_frame, N_FEATURES
    save_ml_model_health = None  # type: ignore[assignment]

_XGB_PATH = Path(MODELS_DIR) / "xgboost_model.json"
_LGB_PATH = Path(MODELS_DIR) / "lightgbm_model.txt"
_SCALER_PATH = Path(MODELS_DIR) / "ml_scaler.joblib"

_xgb_model: Optional[Any] = None   # xgb.Booster
_lgb_model: Optional[Any] = None   # lgb.Booster
_scaler: Optional[Any] = None      # sklearn StandardScaler

# health counters (process-local)
_pred_count = 0
_err_count = 0
_lat_sum_ms = 0.0
_last_health_flush = 0.0
_HEALTH_FLUSH_SECONDS = 300.0  # 5 minutes


def _load_scaler() -> Optional[Any]:
    global _scaler
    if _scaler is not None:
        return _scaler
    if not _JOBLIB_AVAILABLE:
        return None
    if not _SCALER_PATH.exists():
        return None
    try:
        _scaler = joblib.load(_SCALER_PATH)
        log.info("ML scaler loaded from %s", _SCALER_PATH)
    except Exception as e:
        log.warning("Failed to load ML scaler: %s", e)
        _scaler = None
    return _scaler


def _get_xgb() -> Optional[Any]:
    global _xgb_model
    if _xgb_model is not None:
        return _xgb_model
    if not _XGB_AVAILABLE:
        return None
    if _XGB_PATH.exists():
        try:
            bst = xgb.Booster()
            bst.load_model(str(_XGB_PATH))
            _xgb_model = bst
            log.info("XGBoost model loaded from %s", _XGB_PATH)
        except Exception as e:
            log.warning("Failed to load XGBoost model: %s", e)
    return _xgb_model


def _get_lgb() -> Optional[Any]:
    global _lgb_model
    if _lgb_model is not None:
        return _lgb_model
    if not _LGB_AVAILABLE:
        return None
    if _LGB_PATH.exists():
        try:
            _lgb_model = lgb.Booster(model_file=str(_LGB_PATH))
            log.info("LightGBM model loaded from %s", _LGB_PATH)
        except Exception as e:
            log.warning("Failed to load LightGBM model: %s", e)
    return _lgb_model


def is_model_available() -> bool:
    """Return True only if all model files exist AND are loadable."""
    if not (_XGB_PATH.exists() and _LGB_PATH.exists() and _SCALER_PATH.exists()):
        return False
    # Validate that each file is actually loadable (catches corruption)
    try:
        if _XGB_AVAILABLE:
            _bst = xgb.Booster()
            _bst.load_model(str(_XGB_PATH))
        if _LGB_AVAILABLE:
            lgb.Booster(model_file=str(_LGB_PATH))
        if _JOBLIB_AVAILABLE:
            joblib.load(_SCALER_PATH)
        return True
    except Exception:
        return False


def _maybe_flush_health(model_type: str, is_loaded: bool) -> None:
    global _last_health_flush, _lat_sum_ms, _pred_count, _err_count
    now = time.time()
    if now - _last_health_flush < _HEALTH_FLUSH_SECONDS:
        return
    _last_health_flush = now
    if save_ml_model_health is None:
        return
    avg_ms = (_lat_sum_ms / _pred_count) if _pred_count > 0 else 0.0
    try:
        save_ml_model_health(
            model_type=model_type,
            is_loaded=is_loaded,
            avg_prediction_time_ms=avg_ms,
            predictions_count=_pred_count,
            errors_count=_err_count,
            last_error_message="",
        )
    except Exception:
        # Never break trading for health telemetry
        pass


def predict_win_probability(sig: Any, frame_df=None) -> Optional[float]:
    """Predict the probability (0.0–1.0) that the signal trades profitably.

    Returns None if model is not trained yet (new bot, not enough data).
    """
    # Require enriched frame context for canonical feature extraction.
    # If unavailable, bypass ML filter safely.
    if frame_df is None:
        return None

    start = time.perf_counter()
    xgb_bst = _get_xgb()
    lgb_bst = _get_lgb()
    scaler = _load_scaler()
    if xgb_bst is None and lgb_bst is None:
        return None
    try:
        vec = extract_from_signal_and_frame(sig, frame_df).reshape(1, -1)
        if vec.shape[1] != N_FEATURES:
            return None
        if scaler is not None:
            try:
                vec = scaler.transform(vec)
            except Exception:
                # If scaler breaks, fall back to raw features
                pass

        preds: list[float] = []
        if xgb_bst is not None:
            dmat = xgb.DMatrix(vec, feature_names=None)
            preds.append(float(xgb_bst.predict(dmat)[0]))
        if lgb_bst is not None:
            preds.append(float(lgb_bst.predict(vec)[0]))

        if not preds:
            return None

        prob = float(np.mean(preds))
        prob = max(0.0, min(1.0, prob))

        # health accounting
        global _pred_count, _lat_sum_ms
        _pred_count += 1
        _lat_sum_ms += (time.perf_counter() - start) * 1000.0
        _maybe_flush_health("ensemble" if len(preds) > 1 else ("xgboost" if xgb_bst else "lightgbm"), True)

        return prob
    except Exception as e:
        global _err_count
        _err_count += 1
        log.error("ML predict failed: %s", e)
        _maybe_flush_health("ensemble", False)
        return None


def passes_ml_filter(sig: Any) -> tuple[bool, Optional[float]]:
    """Check if a signal passes the ML confidence threshold.

    Returns (passed: bool, confidence: Optional[float]).
    If the model is not available, passes by default (no false rejection).
    """
    prob = predict_win_probability(sig, frame_df=getattr(sig, "_ml_frame", None))
    if prob is None:
        return True, None   # model not trained yet — allow through
    passed = prob >= ML_CONFIDENCE_THRESHOLD
    return passed, prob


def save_xgb_model(booster: Any) -> None:
    global _xgb_model
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    booster.save_model(str(_XGB_PATH))
    _xgb_model = booster
    log.info("XGBoost model saved to %s", _XGB_PATH)


def save_lgb_model(booster: Any) -> None:
    global _lgb_model
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    booster.save_model(str(_LGB_PATH))
    _lgb_model = booster
    log.info("LightGBM model saved to %s", _LGB_PATH)


# Backward-compatible alias (older code expects save_model for XGBoost)
def save_model(booster: Any) -> None:  # noqa: D401
    """Alias for saving the XGBoost model."""
    save_xgb_model(booster)


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
