"""Regime-aware strategy adaptation — Phase 12.

Provides pure-function adapters that adjust scoring, entry filtering,
exit parameters, and sizing based on the current macro-regime context.

All functions are fail-open: if context is None, not confident, or UNCERTAIN,
they return base/unmodified values.  No function ever raises.

Usage (in scanner pipeline)::

    from src.signals.regime_adapter import (
        apply_regime_score_bias,
        check_regime_entry_filter,
        adapt_exit_params,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Safe config loader ────────────────────────────────────────────────────────

def _cfg(attr: str, default: Any = None) -> Any:
    try:
        import src.config as _c
        return getattr(_c, attr, default)
    except Exception:
        return default


# ── Per-asset regime bias overrides ────────────────────────────────────────────

_ASSET_REGIME_OVERRIDES = {
    # Crypto: prefer tighter thresholds in HIGH_VOL (high-volatility assets)
    "BTCUSD": {"HIGH_VOL": {"REGIME_ENTRY_MIN_SCORE_HIGH_VOL": 55.0}},
    "ETHUSD": {"HIGH_VOL": {"REGIME_ENTRY_MIN_SCORE_HIGH_VOL": 55.0}},
    "SOLUSD": {"HIGH_VOL": {"REGIME_ENTRY_MIN_SCORE_HIGH_VOL": 60.0}},
    "AVAXUSD": {"HIGH_VOL": {"REGIME_ENTRY_MIN_SCORE_HIGH_VOL": 58.0}},

    # High-beta stocks: prefer higher thresholds during RANGING
    "NVDA": {"RANGING": {"REGIME_ENTRY_MIN_SCORE_RANGING": 60.0}},
    "TSLA": {"RANGING": {"REGIME_ENTRY_MIN_SCORE_RANGING": 60.0}},
    "META": {"RANGING": {"REGIME_ENTRY_MIN_SCORE_RANGING": 58.0}},

    # Tech ETFs: relax in TRENDING
    "QQQ": {"TRENDING": {"REGIME_ENTRY_MIN_SCORE_TRENDING": 25.0}},
    "TQQQ": {"TRENDING": {"REGIME_ENTRY_MIN_SCORE_TRENDING": 25.0}},
}


def get_asset_regime_override(asset: str, regime: str, param: str) -> Optional[float]:
    """Return asset-specific regime override for a parameter, or None."""
    try:
        overrides = _ASSET_REGIME_OVERRIDES.get(asset)
        if overrides:
            regime_overrides = overrides.get(regime)
            if regime_overrides:
                return regime_overrides.get(param)
    except Exception:
        pass
    return None


# ── Macro-regime helpers ──────────────────────────────────────────────────────

def _get_macro_labels(ctx: Any) -> frozenset:
    """Return set of MacroRegime facets, or empty frozenset."""
    try:
        return ctx.macro_labels()
    except Exception:
        return frozenset()


def _is_confident(ctx: Any) -> bool:
    try:
        return ctx.is_confident(
            min_confidence=float(_cfg("REGIME_MIN_CONFIDENCE", 0.40))
        )
    except Exception:
        return False


# ── 12.3: Score bias ─────────────────────────────────────────────────────────

def apply_regime_score_bias(
    sig: Any,
    regime_ctx: Optional[Any],
) -> None:
    """Add regime-based additive score adjustment to sig.score_total in-place.

    Sets ``regime_ctx.regime_score_adjustment`` and
    ``regime_ctx.regime_score_reason`` for observability.
    Does nothing when regime is absent or not confident.
    """
    if regime_ctx is None or not _is_confident(regime_ctx):
        return
    try:
        from src.signals.regime_types import MacroRegime

        macros = _get_macro_labels(regime_ctx)
        if not macros:
            return

        bias = 0.0
        reasons: list[str] = []

        _bias_map = {
            MacroRegime.TRENDING:  float(_cfg("REGIME_SCORE_BIAS_TRENDING",  3.0)),
            MacroRegime.RANGING:   float(_cfg("REGIME_SCORE_BIAS_RANGING",  -5.0)),
            MacroRegime.HIGH_VOL:  float(_cfg("REGIME_SCORE_BIAS_HIGH_VOL", -3.0)),
            MacroRegime.LOW_VOL:   float(_cfg("REGIME_SCORE_BIAS_LOW_VOL",  -2.0)),
            MacroRegime.UNCERTAIN: float(_cfg("REGIME_SCORE_BIAS_UNCERTAIN", 0.0)),
        }

        for macro in sorted(macros, key=lambda m: m.value):
            delta = _bias_map.get(macro, 0.0)
            if delta != 0.0:
                bias += delta
                reasons.append(f"{macro.value}={delta:+.1f}")

        if bias == 0.0:
            return

        old_score = float(getattr(sig, "score_total", 0.0))
        sig.score_total = old_score + bias

        regime_ctx.regime_score_adjustment = bias
        regime_ctx.regime_score_reason = ", ".join(reasons)

        log.info(
            "Regime score bias: %s %s/%s score %.1f → %.1f (bias=%+.1f: %s)",
            getattr(sig, "asset", "?"),
            getattr(sig, "timeframe", "?"),
            getattr(sig, "signal_type", "?"),
            old_score,
            sig.score_total,
            bias,
            regime_ctx.regime_score_reason,
        )
    except Exception as exc:
        log.debug("apply_regime_score_bias failed: %s", exc)


# ── 12.4: Entry filter ───────────────────────────────────────────────────────

def check_regime_entry_filter(
    sig: Any,
    regime_ctx: Optional[Any],
    extra_threshold_delta: float = 0.0,
) -> tuple[bool, str]:
    """Check whether the current regime allows entry for this signal.

    Returns (allowed, reason).  When the filter is disabled or context is
    absent, returns (True, "").

    Applies per-asset regime overrides if available.

    Parameters
    ----------
    extra_threshold_delta :
        Additional score-points added on top of the regime base minimum
        (supplied by Phase 14 suitability gating).  Positive = stricter.
    """
    if not _cfg("REGIME_ENTRY_FILTER_ENABLED", True):
        return True, ""

    if regime_ctx is None or not _is_confident(regime_ctx):
        return True, "regime_not_confident"

    try:
        from src.signals.regime_types import MacroRegime

        macros = _get_macro_labels(regime_ctx)
        score = float(getattr(sig, "score_total", 0.0))
        asset = str(getattr(sig, "asset", ""))

        _min_score_map = {
            MacroRegime.TRENDING:  float(_cfg("REGIME_ENTRY_MIN_SCORE_TRENDING",  30.0)),
            MacroRegime.RANGING:   float(_cfg("REGIME_ENTRY_MIN_SCORE_RANGING",   50.0)),
            MacroRegime.HIGH_VOL:  float(_cfg("REGIME_ENTRY_MIN_SCORE_HIGH_VOL",  45.0)),
            MacroRegime.LOW_VOL:   float(_cfg("REGIME_ENTRY_MIN_SCORE_LOW_VOL",   40.0)),
            MacroRegime.UNCERTAIN: float(_cfg("REGIME_ENTRY_MIN_SCORE_UNCERTAIN", 35.0)),
        }

        # Use strictest threshold among all active facets, then add Phase 14 delta
        effective_min = 0.0
        governing_facet = ""
        for macro in macros:
            param_key = f"REGIME_ENTRY_MIN_SCORE_{macro.value}"
            # Check for per-asset override first
            override_val = get_asset_regime_override(asset, macro.value, param_key)
            threshold = override_val if override_val is not None else _min_score_map.get(macro, 0.0)
            if threshold > effective_min:
                effective_min = threshold
                governing_facet = macro.value

        effective_min += extra_threshold_delta

        if score < effective_min:
            reason = (
                f"regime_entry_rejected: score={score:.1f} < "
                f"min={effective_min:.1f} ({governing_facet}"
                + (f"+suitability_delta={extra_threshold_delta:.1f}" if extra_threshold_delta else "")
                + ")"
            )
            regime_ctx.regime_entry_allowed = False
            regime_ctx.regime_entry_reason = reason
            return False, reason

        reason = f"regime_entry_allowed: score={score:.1f} >= min={effective_min:.1f}"
        regime_ctx.regime_entry_allowed = True
        regime_ctx.regime_entry_reason = reason
        return True, reason

    except Exception as exc:
        log.debug("check_regime_entry_filter failed: %s", exc)
        return True, ""


# ── 12.5: Exit parameter adaptation ──────────────────────────────────────────

def adapt_exit_params(
    regime_ctx: Optional[Any],
    giveback_frac: float,
    break_even_pct: float,
    fade_tighten_frac: float,
) -> tuple[float, float, float, str]:
    """Return regime-adapted (giveback_frac, break_even_pct, fade_tighten_frac, reason).

    All multipliers are applied from config; original values returned if
    regime is absent or not confident.
    """
    if regime_ctx is None or not _is_confident(regime_ctx):
        return giveback_frac, break_even_pct, fade_tighten_frac, ""

    try:
        from src.signals.regime_types import MacroRegime

        macros = _get_macro_labels(regime_ctx)
        if not macros:
            return giveback_frac, break_even_pct, fade_tighten_frac, ""

        gb_mult = 1.0
        be_mult = 1.0
        fade_mult = 1.0
        parts: list[str] = []

        _gb_map = {
            MacroRegime.TRENDING:  float(_cfg("REGIME_EXIT_GIVEBACK_MULT_TRENDING",  1.20)),
            MacroRegime.RANGING:   float(_cfg("REGIME_EXIT_GIVEBACK_MULT_RANGING",   0.80)),
            MacroRegime.HIGH_VOL:  float(_cfg("REGIME_EXIT_GIVEBACK_MULT_HIGH_VOL",  0.85)),
            MacroRegime.LOW_VOL:   float(_cfg("REGIME_EXIT_GIVEBACK_MULT_LOW_VOL",   1.10)),
        }
        _be_map = {
            MacroRegime.TRENDING:  float(_cfg("REGIME_EXIT_BE_MULT_TRENDING",  1.10)),
            MacroRegime.RANGING:   float(_cfg("REGIME_EXIT_BE_MULT_RANGING",   0.80)),
            MacroRegime.HIGH_VOL:  float(_cfg("REGIME_EXIT_BE_MULT_HIGH_VOL",  0.75)),
            MacroRegime.LOW_VOL:   float(_cfg("REGIME_EXIT_BE_MULT_LOW_VOL",   1.15)),
        }
        _fade_map = {
            MacroRegime.TRENDING:  float(_cfg("REGIME_EXIT_FADE_MULT_TRENDING",  0.80)),
            MacroRegime.RANGING:   float(_cfg("REGIME_EXIT_FADE_MULT_RANGING",   1.25)),
            MacroRegime.HIGH_VOL:  float(_cfg("REGIME_EXIT_FADE_MULT_HIGH_VOL",  1.15)),
            MacroRegime.LOW_VOL:   float(_cfg("REGIME_EXIT_FADE_MULT_LOW_VOL",   0.90)),
        }

        for macro in sorted(macros, key=lambda m: m.value):
            if macro in _gb_map:
                gb_mult *= _gb_map[macro]
            if macro in _be_map:
                be_mult *= _be_map[macro]
            if macro in _fade_map:
                fade_mult *= _fade_map[macro]
            parts.append(macro.value)

        new_gb = round(giveback_frac * gb_mult, 4)
        new_be = round(break_even_pct * be_mult, 4)
        new_fade = round(fade_tighten_frac * fade_mult, 4)

        # Safety clamps
        new_gb = max(0.10, min(0.80, new_gb))
        new_be = max(0.10, min(3.00, new_be))
        new_fade = max(0.10, min(0.60, new_fade))

        reason = "+".join(parts)
        return new_gb, new_be, new_fade, reason

    except Exception as exc:
        log.debug("adapt_exit_params failed: %s", exc)
        return giveback_frac, break_even_pct, fade_tighten_frac, ""
