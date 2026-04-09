"""Local ML training pipeline (Supabase/SQLite-backed).

Trains:
  - XGBoost (primary)
  - LightGBM (secondary)

Data source:
  - ``ml_features`` table (mirrored to Supabase when configured)

Training requirements (guardrails):
  - minimum 200 closed trades in ml_features
  - minimum 100 winners and 100 losers (class balance)
  - time-aware validation split (no random shuffle leakage)

Artifacts:
  - models/xgboost_model.json
  - models/lightgbm_model.txt
  - models/ml_scaler.joblib
  - models/ml_metadata.json

CLI:
  - python -m src.ml.train --status
  - python -m src.ml.train --train [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    from src.data.db              import get_ml_training_data, save_ml_features, get_closed_trades
    from src.ml.features          import extract_from_trade_dict, outcome_from_trade, N_FEATURES, FEATURE_VERSION
    from src.ml.model             import train_model, save_xgb_model, save_lgb_model, is_model_available
    from src.config               import ML_CONFIDENCE_THRESHOLD, MODELS_DIR
except ImportError as e:
    log.error("Import error in ml.train: %s", e)
    raise

MIN_SAMPLES = 200
MIN_WINS = 100
MIN_LOSSES = 100

_META_PATH = Path(MODELS_DIR) / "ml_metadata.json"

# Auto-retrain policy
AUTO_RETRAIN_MIN_CLOSED_TRADES = 1000
AUTO_RETRAIN_DAYS = 7
AUTO_RETRAIN_MIN_NEW_TRADES = 50

_last_auto_check = 0.0
_AUTO_CHECK_SECONDS = 300.0


def _collect_training_rows() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Pull ml_features rows from DB and return (X, y, rows) in chronological order."""
    rows = get_ml_training_data()
    if not rows:
        return np.empty((0, N_FEATURES)), np.empty(0), []

    # Prefer time-order to avoid leakage. SQLite created_at is ISO-ish; Supabase should match.
    def _ts(r: dict) -> str:
        return str(r.get("created_at") or r.get("timestamp") or "")

    rows_sorted = sorted(rows, key=_ts)

    vectors: list[np.ndarray] = []
    labels: list[float] = []
    for row in rows_sorted:
        try:
            vec = extract_from_trade_dict(row)
            if len(vec) == N_FEATURES:
                vectors.append(vec)
                labels.append(float(row["outcome"]))
        except Exception as e:
            log.warning("Skipping corrupt ml_features row %s: %s", row.get("id"), e)
    if not vectors:
        return np.empty((0, N_FEATURES)), np.empty(0), rows_sorted
    return np.vstack(vectors), np.array(labels), rows_sorted


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
            save_ml_features(trade["trade_id"], vec.tolist(), outcome)
            added += 1
        except Exception as e:
            log.debug("backfill skip %s: %s", trade.get("trade_id"), e)
    log.info("Backfilled %d feature rows from closed trades", added)
    return added


def run_training(force: bool = False) -> dict:
    """Train (or retrain) the ML filter model.

    Returns a status dict with keys: trained, accuracy, n_samples, message.
    """
    X, y, rows = _collect_training_rows()
    n = len(y)

    wins = int(y.sum()) if n > 0 else 0
    losses = n - wins

    if (n < MIN_SAMPLES or wins < MIN_WINS or losses < MIN_LOSSES) and not force:
        msg = (
            f"Not enough balanced data: total={n}/{MIN_SAMPLES}, "
            f"wins={wins}/{MIN_WINS}, losses={losses}/{MIN_LOSSES}."
        )
        log.warning(msg)
        return {"trained": False, "metrics": None, "n_samples": n, "wins": wins, "losses": losses, "message": msg}

    if n < MIN_SAMPLES or wins < MIN_WINS or losses < MIN_LOSSES:
        log.warning("Forcing training with insufficient balance (--force): total=%d wins=%d losses=%d", n, wins, losses)

    # 80/20 split
    split   = max(1, int(n * 0.8))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    # Scale
    scaler = None
    try:
        from sklearn.preprocessing import StandardScaler
        import joblib
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_val_s = scaler.transform(X_val) if len(X_val) else X_val
        Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
        joblib.dump(scaler, str(Path(MODELS_DIR) / "ml_scaler.joblib"))
    except Exception as e:
        log.warning("Scaler unavailable, training on raw features: %s", e)
        X_train_s, X_val_s = X_train, X_val

    metrics: dict = {"xgboost": None, "lightgbm": None, "chosen": None}

    # Optional Optuna tuning (only when enough data is available)
    tuned_params = None
    if n >= 500:
        try:
            import optuna
            import xgboost as xgb
            from sklearn.metrics import roc_auc_score

            def objective(trial: optuna.Trial) -> float:
                params = {
                    "eta": trial.suggest_float("eta", 0.01, 0.2, log=True),
                    "max_depth": trial.suggest_int("max_depth", 3, 8),
                    "subsample": trial.suggest_float("subsample", 0.6, 1.0),
                    "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
                    "min_child_weight": trial.suggest_int("min_child_weight", 1, 8),
                }
                bst = train_model(X_train_s, y_train, params=params)
                if len(X_val_s) == 0:
                    return 0.0
                dval = xgb.DMatrix(X_val_s)
                p = bst.predict(dval)
                return float(roc_auc_score(y_val.astype(int), p))

            study = optuna.create_study(direction="maximize")
            study.optimize(objective, n_trials=30)
            tuned_params = study.best_params
            log.info("Optuna tuned params: %s", tuned_params)
        except Exception as e:
            log.warning("Optuna tuning skipped/failed: %s", e)

    # Train XGBoost (use tuned params when available)
    xgb_bst = train_model(X_train_s, y_train, params=tuned_params)
    xgb_auc = None
    if len(X_val_s):
        try:
            import xgboost as xgb
            from sklearn.metrics import roc_auc_score
            dval = xgb.DMatrix(X_val_s)
            p = xgb_bst.predict(dval)
            xgb_auc = float(roc_auc_score(y_val.astype(int), p))
            metrics["xgboost"] = {"auc": xgb_auc}
        except Exception as e:
            log.warning("XGBoost validation failed: %s", e)
            metrics["xgboost"] = {"auc": None}
    save_xgb_model(xgb_bst)

    # Train LightGBM if available
    lgb_bst = None
    try:
        import lightgbm as lgb
        dtrain = lgb.Dataset(X_train_s, label=y_train.astype(int))
        params = {
            "objective": "binary",
            "metric": "auc",
            "learning_rate": 0.05,
            "num_leaves": 31,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.9,
            "bagging_freq": 1,
            "verbosity": -1,
            "seed": 42,
        }
        lgb_bst = lgb.train(params, dtrain, num_boost_round=300)
        lgb_auc = None
        if len(X_val_s):
            from sklearn.metrics import roc_auc_score
            lp = lgb_bst.predict(X_val_s)
            lgb_auc = float(roc_auc_score(y_val.astype(int), lp))
            metrics["lightgbm"] = {"auc": lgb_auc}
        save_lgb_model(lgb_bst)
    except Exception as e:
        log.warning("LightGBM training skipped/failed: %s", e)

    # Choose best by AUC (fallback: xgb)
    chosen = "xgboost"
    if metrics["lightgbm"] and metrics["lightgbm"].get("auc") is not None and metrics["xgboost"] and metrics["xgboost"].get("auc") is not None:
        if metrics["lightgbm"]["auc"] > metrics["xgboost"]["auc"]:
            chosen = "lightgbm"
    metrics["chosen"] = chosen

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_version": FEATURE_VERSION,
        "n_samples": n,
        "wins": wins,
        "losses": losses,
        "train_size": split,
        "val_size": len(X_val),
        "threshold": ML_CONFIDENCE_THRESHOLD,
        "metrics": metrics,
    }
    try:
        Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
        _META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass

    msg = f"Trained XGB/LGB on {split} samples (val={len(X_val)}). chosen={chosen}."
    log.info("ML training complete: %s", msg)
    return {"trained": True, "metrics": metrics, "n_samples": n, "wins": wins, "losses": losses, "message": msg}


def get_ml_status() -> dict:
    """Return current ML model status for display."""
    X, y, _ = _collect_training_rows()
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


def _read_meta() -> dict:
    try:
        if _META_PATH.exists():
            return json.loads(_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def should_auto_retrain() -> tuple[bool, str]:
    """Decide whether to auto-retrain now based on cadence + new data count."""
    meta = _read_meta()
    st = get_ml_status()
    total = int(st.get("total_samples", 0))

    if total < AUTO_RETRAIN_MIN_CLOSED_TRADES:
        return False, f"need_total_samples>={AUTO_RETRAIN_MIN_CLOSED_TRADES}"

    last_trained = meta.get("trained_at")
    last_n = int(meta.get("n_samples", 0) or 0)
    new_trades = max(0, total - last_n)
    if new_trades < AUTO_RETRAIN_MIN_NEW_TRADES:
        return False, f"need_new_trades>={AUTO_RETRAIN_MIN_NEW_TRADES} (have {new_trades})"

    if not last_trained:
        return True, "no_previous_training"

    try:
        dt = datetime.fromisoformat(str(last_trained).replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).days
        if age_days >= AUTO_RETRAIN_DAYS:
            return True, f"age_days={age_days}"
        return False, f"need_age_days>={AUTO_RETRAIN_DAYS} (age_days={age_days})"
    except Exception:
        return True, "bad_metadata_timestamp"


def maybe_auto_retrain() -> Optional[dict]:
    """Run auto-retraining if policy triggers. Safe to call frequently."""
    global _last_auto_check
    import time
    now = time.time()
    if now - _last_auto_check < _AUTO_CHECK_SECONDS:
        return None
    _last_auto_check = now

    ok, reason = should_auto_retrain()
    if not ok:
        log.debug("Auto-retrain skipped: %s", reason)
        return None

    log.info("Auto-retrain triggered: %s", reason)
    return run_training(force=False)


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ml.train")
    parser.add_argument("--status", action="store_true", help="Show ML training readiness and model availability")
    parser.add_argument("--train", action="store_true", help="Run (re)training now")
    parser.add_argument("--force", action="store_true", help="Force training even if guards fail")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(get_ml_status(), indent=2))
        return 0
    if args.train:
        print(json.dumps(run_training(force=args.force), indent=2))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
