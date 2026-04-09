"""Regime-aware signal/risk soft hooks — Phase 11.

Provides configurable, fail-open modifiers that the scanner applies to:
  * ML confidence threshold
  * AI confidence threshold
  * Position size

All functions return unmodified base values when:
  * regime_context is None
  * regime label is UNKNOWN
  * regime confidence is below the configured minimum

No function ever raises — all exceptions are caught and the base value
returned, so the trading pipeline is never blocked by regime logic.

Design principle
----------------
These are *advisory biases* not hard gates.  The first rollout uses soft
controls only; the architecture is ready for future hard-gating by adding a
config flag without restructuring this module.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.signals.regime_types import RegimeContext, RegimeLabel

log = logging.getLogger(__name__)

# ── Config import with safe defaults ─────────────────────────────────────────

def _load_config() -> dict:
    """Return a snapshot of relevant regime config values."""
    try:
        import src.config as _cfg
        return {
            "REGIME_MIN_CONFIDENCE":            getattr(_cfg, "REGIME_MIN_CONFIDENCE",            0.40),
            "REGIME_ML_THRESHOLD_MIN":          getattr(_cfg, "REGIME_ML_THRESHOLD_MIN",          0.45),
            "REGIME_ML_THRESHOLD_MAX":          getattr(_cfg, "REGIME_ML_THRESHOLD_MAX",          0.90),
            "REGIME_AI_THRESHOLD_MIN":          getattr(_cfg, "REGIME_AI_THRESHOLD_MIN",          0.40),
            "REGIME_AI_THRESHOLD_MAX":          getattr(_cfg, "REGIME_AI_THRESHOLD_MAX",          0.90),
            "REGIME_SIZE_FACTOR_MIN":           getattr(_cfg, "REGIME_SIZE_FACTOR_MIN",           0.40),
            "REGIME_SIZE_FACTOR_MAX":           getattr(_cfg, "REGIME_SIZE_FACTOR_MAX",           1.25),
            # Per-label deltas
            "REGIME_CHOPPY_LOW_VOL_ML_DELTA":    getattr(_cfg, "REGIME_CHOPPY_LOW_VOL_ML_DELTA",   0.05),
            "REGIME_CHOPPY_LOW_VOL_AI_DELTA":    getattr(_cfg, "REGIME_CHOPPY_LOW_VOL_AI_DELTA",   0.05),
            "REGIME_CHOPPY_LOW_VOL_SIZE_FACTOR": getattr(_cfg, "REGIME_CHOPPY_LOW_VOL_SIZE_FACTOR",0.75),
            "REGIME_CHOPPY_HIGH_VOL_ML_DELTA":   getattr(_cfg, "REGIME_CHOPPY_HIGH_VOL_ML_DELTA",  0.07),
            "REGIME_CHOPPY_HIGH_VOL_AI_DELTA":   getattr(_cfg, "REGIME_CHOPPY_HIGH_VOL_AI_DELTA",  0.07),
            "REGIME_CHOPPY_HIGH_VOL_SIZE_FACTOR":getattr(_cfg, "REGIME_CHOPPY_HIGH_VOL_SIZE_FACTOR",0.65),
            "REGIME_NEWS_UNSTABLE_ML_DELTA":     getattr(_cfg, "REGIME_NEWS_UNSTABLE_ML_DELTA",    0.10),
            "REGIME_NEWS_UNSTABLE_AI_DELTA":     getattr(_cfg, "REGIME_NEWS_UNSTABLE_AI_DELTA",    0.10),
            "REGIME_NEWS_UNSTABLE_SIZE_FACTOR":  getattr(_cfg, "REGIME_NEWS_UNSTABLE_SIZE_FACTOR", 0.50),
            "REGIME_REVERSAL_ML_DELTA":          getattr(_cfg, "REGIME_REVERSAL_ML_DELTA",         0.05),
            "REGIME_REVERSAL_AI_DELTA":          getattr(_cfg, "REGIME_REVERSAL_AI_DELTA",         0.05),
            "REGIME_REVERSAL_SIZE_FACTOR":       getattr(_cfg, "REGIME_REVERSAL_SIZE_FACTOR",      0.80),
            "REGIME_TRENDING_HIGH_VOL_ML_DELTA":    getattr(_cfg, "REGIME_TRENDING_HIGH_VOL_ML_DELTA",   -0.03),
            "REGIME_TRENDING_HIGH_VOL_AI_DELTA":    getattr(_cfg, "REGIME_TRENDING_HIGH_VOL_AI_DELTA",   -0.03),
            "REGIME_TRENDING_HIGH_VOL_SIZE_FACTOR": getattr(_cfg, "REGIME_TRENDING_HIGH_VOL_SIZE_FACTOR", 1.10),
            "REGIME_TRENDING_LOW_VOL_ML_DELTA":     getattr(_cfg, "REGIME_TRENDING_LOW_VOL_ML_DELTA",    0.0),
            "REGIME_TRENDING_LOW_VOL_AI_DELTA":     getattr(_cfg, "REGIME_TRENDING_LOW_VOL_AI_DELTA",    0.0),
            "REGIME_TRENDING_LOW_VOL_SIZE_FACTOR":  getattr(_cfg, "REGIME_TRENDING_LOW_VOL_SIZE_FACTOR",  1.0),
        }
    except Exception:
        return {}   # all lookups below have defaults


def _label_str(ctx: Any) -> str:
    """Return the regime label string from a RegimeContext (safe)."""
    try:
        lbl = ctx.regime_label
        return lbl.value if hasattr(lbl, "value") else str(lbl)
    except Exception:
        return "UNKNOWN"


def _is_confident(ctx: Any, cfg: dict) -> bool:
    """True when regime context passes minimum confidence gate."""
    try:
        label = _label_str(ctx)
        if label == "UNKNOWN":
            return False
        min_conf = cfg.get("REGIME_MIN_CONFIDENCE", 0.40)
        return float(ctx.confidence_score) >= min_conf
    except Exception:
        return False


# ── Per-label modifier lookup ─────────────────────────────────────────────────

def _get_label_modifiers(label: str, cfg: dict) -> tuple[float, float, float]:
    """Return (ml_delta, ai_delta, size_factor) for the given label string."""
    if label == "CHOPPY_LOW_VOL":
        return (
            cfg.get("REGIME_CHOPPY_LOW_VOL_ML_DELTA",    0.05),
            cfg.get("REGIME_CHOPPY_LOW_VOL_AI_DELTA",    0.05),
            cfg.get("REGIME_CHOPPY_LOW_VOL_SIZE_FACTOR", 0.75),
        )
    if label == "CHOPPY_HIGH_VOL":
        return (
            cfg.get("REGIME_CHOPPY_HIGH_VOL_ML_DELTA",    0.07),
            cfg.get("REGIME_CHOPPY_HIGH_VOL_AI_DELTA",    0.07),
            cfg.get("REGIME_CHOPPY_HIGH_VOL_SIZE_FACTOR", 0.65),
        )
    if label == "NEWS_DRIVEN_UNSTABLE":
        return (
            cfg.get("REGIME_NEWS_UNSTABLE_ML_DELTA",    0.10),
            cfg.get("REGIME_NEWS_UNSTABLE_AI_DELTA",    0.10),
            cfg.get("REGIME_NEWS_UNSTABLE_SIZE_FACTOR", 0.50),
        )
    if label == "REVERSAL_TRANSITION":
        return (
            cfg.get("REGIME_REVERSAL_ML_DELTA",    0.05),
            cfg.get("REGIME_REVERSAL_AI_DELTA",    0.05),
            cfg.get("REGIME_REVERSAL_SIZE_FACTOR", 0.80),
        )
    if label == "TRENDING_HIGH_VOL":
        return (
            cfg.get("REGIME_TRENDING_HIGH_VOL_ML_DELTA",    -0.03),
            cfg.get("REGIME_TRENDING_HIGH_VOL_AI_DELTA",    -0.03),
            cfg.get("REGIME_TRENDING_HIGH_VOL_SIZE_FACTOR",  1.10),
        )
    if label == "TRENDING_LOW_VOL":
        return (
            cfg.get("REGIME_TRENDING_LOW_VOL_ML_DELTA",    0.0),
            cfg.get("REGIME_TRENDING_LOW_VOL_AI_DELTA",    0.0),
            cfg.get("REGIME_TRENDING_LOW_VOL_SIZE_FACTOR", 1.0),
        )
    # UNKNOWN or unrecognised → no modifier
    return 0.0, 0.0, 1.0


# ── Public hook functions ─────────────────────────────────────────────────────

def resolve_ml_threshold(base: float, regime_context: Optional[Any]) -> float:
    """Return the ML confidence threshold adjusted by regime.

    Parameters
    ----------
    base :
        The baseline threshold from config (e.g. 0.65).
    regime_context :
        A ``RegimeContext`` instance or None.

    Returns
    -------
    float
        Adjusted threshold clamped to [REGIME_ML_THRESHOLD_MIN, REGIME_ML_THRESHOLD_MAX].
        Returns *base* unchanged when regime is absent or not confident.
    """
    try:
        if regime_context is None:
            return base
        cfg = _load_config()
        if not _is_confident(regime_context, cfg):
            return base
        label = _label_str(regime_context)
        ml_delta, _, _ = _get_label_modifiers(label, cfg)
        adjusted = base + ml_delta
        clamped = float(max(
            cfg.get("REGIME_ML_THRESHOLD_MIN", 0.45),
            min(cfg.get("REGIME_ML_THRESHOLD_MAX", 0.90), adjusted),
        ))
        if clamped != base:
            log.debug("regime ML threshold: base=%.2f delta=%.2f → %.2f [%s]",
                      base, ml_delta, clamped, label)
        return clamped
    except Exception as exc:
        log.debug("resolve_ml_threshold failed: %s", exc)
        return base


def resolve_ai_threshold(base: float, regime_context: Optional[Any]) -> float:
    """Return the AI confidence threshold adjusted by regime.

    Mirrors ``resolve_ml_threshold`` — identical semantics, separate knobs.
    """
    try:
        if regime_context is None:
            return base
        cfg = _load_config()
        if not _is_confident(regime_context, cfg):
            return base
        label = _label_str(regime_context)
        _, ai_delta, _ = _get_label_modifiers(label, cfg)
        adjusted = base + ai_delta
        clamped = float(max(
            cfg.get("REGIME_AI_THRESHOLD_MIN", 0.40),
            min(cfg.get("REGIME_AI_THRESHOLD_MAX", 0.90), adjusted),
        ))
        if clamped != base:
            log.debug("regime AI threshold: base=%.2f delta=%.2f → %.2f [%s]",
                      base, ai_delta, clamped, label)
        return clamped
    except Exception as exc:
        log.debug("resolve_ai_threshold failed: %s", exc)
        return base


def resolve_position_size_factor(regime_context: Optional[Any]) -> float:
    """Return a multiplicative size factor based on current regime.

    Returns 1.0 when regime is absent or not confident (no change).
    Callers: ``adjusted_size = base_size * resolve_position_size_factor(ctx)``.
    """
    try:
        if regime_context is None:
            return 1.0
        cfg = _load_config()
        if not _is_confident(regime_context, cfg):
            return 1.0
        label = _label_str(regime_context)
        _, _, size_factor = _get_label_modifiers(label, cfg)
        clamped = float(max(
            cfg.get("REGIME_SIZE_FACTOR_MIN", 0.40),
            min(cfg.get("REGIME_SIZE_FACTOR_MAX", 1.25), size_factor),
        ))
        if clamped != 1.0:
            log.debug("regime size factor: %.2f [%s]", clamped, label)
        return clamped
    except Exception as exc:
        log.debug("resolve_position_size_factor failed: %s", exc)
        return 1.0


def populate_regime_modifiers(ctx: Any) -> None:
    """Fill modifier fields on a ``RegimeContext`` in-place from config.

    This is a convenience so the scanner doesn't have to call each resolver
    individually — it calls this once and the context carries the resolved
    values forward.
    """
    try:
        cfg = _load_config()
        if not _is_confident(ctx, cfg):
            return
        label = _label_str(ctx)
        ml_d, ai_d, sf = _get_label_modifiers(label, cfg)
        ctx.ml_threshold_delta   = ml_d
        ctx.ai_threshold_delta   = ai_d
        ctx.position_size_factor = float(max(
            cfg.get("REGIME_SIZE_FACTOR_MIN", 0.40),
            min(cfg.get("REGIME_SIZE_FACTOR_MAX", 1.25), sf),
        ))
    except Exception as exc:
        log.debug("populate_regime_modifiers failed: %s", exc)


def build_regime_context_for_signal(snapshot: Any, previous_snapshot: Any = None) -> Any:
    """Convert a ``RegimeSnapshot`` to a ``RegimeContext`` ready for the pipeline.

    Populates all modifier fields so the scanner gets a single object with
    everything it needs.

    Parameters
    ----------
    snapshot : RegimeSnapshot
        Current regime classification.
    previous_snapshot : RegimeSnapshot, optional
        Previous regime classification (for transition tracking).

    Returns a new ``RegimeContext`` instance.
    """
    try:
        from src.signals.regime_types import RegimeContext
        from datetime import datetime, timezone

        # Phase 12: compute transition metadata
        _prev_label = None
        _duration = 0.0
        if previous_snapshot is not None:
            try:
                _prev_label = previous_snapshot.regime_label
                _prev_ts = previous_snapshot.created_at
                _cur_ts = snapshot.created_at
                if _prev_ts and _cur_ts:
                    _duration = (_cur_ts - _prev_ts).total_seconds()
                    if _duration < 0:
                        _duration = 0.0
            except Exception:
                pass

        ctx = RegimeContext(
            regime_label     = snapshot.regime_label,
            confidence_score = float(snapshot.confidence_score),
            evidence_summary = snapshot.evidence_summary or "",
            snapshot_id      = snapshot.regime_id,
            news_input_present = bool(snapshot.news_instability_flag),
            # Phase 12 enriched fields
            previous_label         = _prev_label,
            regime_duration_seconds = _duration,
            timestamp              = snapshot.created_at if hasattr(snapshot, 'created_at') else None,
            asset                  = getattr(snapshot, 'asset', ''),
            timeframe              = getattr(snapshot, 'timeframe', ''),
        )
        populate_regime_modifiers(ctx)
        return ctx
    except Exception as exc:
        log.debug("build_regime_context_for_signal failed: %s", exc)
        try:
            from src.signals.regime_types import RegimeContext
            return RegimeContext()
        except Exception:
            return None
