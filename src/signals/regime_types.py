"""Regime data model — Phase 11 / Phase 12.

Defines the market-regime label enum, the full RegimeSnapshot record, and the
lightweight RegimeContext carrier that flows through the signal/risk pipeline.

Phase 12 additions
------------------
* ``MacroRegime`` enum — coarse facets (TRENDING, RANGING, HIGH_VOL, LOW_VOL,
  UNCERTAIN) derived from the canonical Phase 11 labels.
* ``RegimeContext`` extended with: ``previous_label``, ``regime_duration_seconds``,
  ``timestamp``, ``asset``, ``timeframe`` for adaptation and transition tracking.

Design rules
------------
* All fields are optional or defaulted so that code written before Phase 11
  continues to work unmodified.
* RegimeContext is intentionally a small plain dataclass — not a dict — so
  attribute access is safe and IDE-friendly.
* RegimeSnapshot is the persistence record.  It maps 1:1 to a row in the
  ``regime_snapshots`` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, FrozenSet, Optional


# ── Macro regime facets (Phase 12) ───────────────────────────────────────────

class MacroRegime(str, Enum):
    """Coarse regime facets used by the adaptation layer.

    A single RegimeLabel can map to multiple MacroRegime facets.
    E.g. TRENDING_HIGH_VOL → {TRENDING, HIGH_VOL}.
    """
    TRENDING  = "TRENDING"
    RANGING   = "RANGING"
    HIGH_VOL  = "HIGH_VOL"
    LOW_VOL   = "LOW_VOL"
    UNCERTAIN = "UNCERTAIN"


# ── Mapping from canonical labels to macro facets ────────────────────────────

_LABEL_TO_MACRO: dict[str, FrozenSet["MacroRegime"]] = {
    "TRENDING_HIGH_VOL":    frozenset({MacroRegime.TRENDING, MacroRegime.HIGH_VOL}),
    "TRENDING_LOW_VOL":     frozenset({MacroRegime.TRENDING, MacroRegime.LOW_VOL}),
    "CHOPPY_HIGH_VOL":      frozenset({MacroRegime.RANGING,  MacroRegime.HIGH_VOL}),
    "CHOPPY_LOW_VOL":       frozenset({MacroRegime.RANGING,  MacroRegime.LOW_VOL}),
    "REVERSAL_TRANSITION":  frozenset({MacroRegime.UNCERTAIN}),
    "NEWS_DRIVEN_UNSTABLE": frozenset({MacroRegime.UNCERTAIN, MacroRegime.HIGH_VOL}),
    "UNKNOWN":              frozenset({MacroRegime.UNCERTAIN}),
}


# ── Regime label ─────────────────────────────────────────────────────────────

class RegimeLabel(str, Enum):
    """Mutually exclusive market-regime classifications."""

    TRENDING_HIGH_VOL    = "TRENDING_HIGH_VOL"
    TRENDING_LOW_VOL     = "TRENDING_LOW_VOL"
    CHOPPY_HIGH_VOL      = "CHOPPY_HIGH_VOL"
    CHOPPY_LOW_VOL       = "CHOPPY_LOW_VOL"
    REVERSAL_TRANSITION  = "REVERSAL_TRANSITION"
    NEWS_DRIVEN_UNSTABLE = "NEWS_DRIVEN_UNSTABLE"
    UNKNOWN              = "UNKNOWN"

    def is_trending(self) -> bool:
        return self in (RegimeLabel.TRENDING_HIGH_VOL, RegimeLabel.TRENDING_LOW_VOL)

    def is_choppy(self) -> bool:
        return self in (RegimeLabel.CHOPPY_HIGH_VOL, RegimeLabel.CHOPPY_LOW_VOL)

    def is_high_vol(self) -> bool:
        return self in (RegimeLabel.TRENDING_HIGH_VOL, RegimeLabel.CHOPPY_HIGH_VOL)

    def is_low_vol(self) -> bool:
        return self in (RegimeLabel.TRENDING_LOW_VOL, RegimeLabel.CHOPPY_LOW_VOL)

    def is_adverse(self) -> bool:
        """True for regimes where default entry trust should be reduced."""
        return self in (
            RegimeLabel.CHOPPY_LOW_VOL,
            RegimeLabel.NEWS_DRIVEN_UNSTABLE,
            RegimeLabel.REVERSAL_TRANSITION,
        )

    def is_unknown(self) -> bool:
        return self is RegimeLabel.UNKNOWN

    def macro_labels(self) -> FrozenSet[MacroRegime]:
        """Return the set of macro-regime facets for this label."""
        return _LABEL_TO_MACRO.get(self.value, frozenset({MacroRegime.UNCERTAIN}))


# ── Volatility / trend / chop metric bundles ─────────────────────────────────

@dataclass
class VolatilityMetrics:
    """ATR-based and dispersion-based volatility features."""
    atr_current:        float = 0.0    # most-recent ATR value
    atr_ratio:          float = 0.0    # atr_current / atr_lookback_mean  (expansion proxy)
    atr_percentile:     float = 0.0    # relative rank in rolling window (0–1)
    range_expansion:    float = 0.0    # recent candle range / lookback mean range
    noise_ratio:        float = 0.0    # wick-to-range ratio; higher = noisier


@dataclass
class TrendMetrics:
    """Trend persistence and structure features."""
    hh_ll_streak:       int   = 0      # positive = consecutive HH/HL; negative = LH/LL
    directional_bars:   float = 0.0    # fraction of bars in dominant direction (0–1)
    follow_through:     float = 0.0    # avg next-bar continuation (0–1)
    breakout_fail_rate: float = 0.0    # fraction of breakouts that failed in window
    trend_strength:     float = 0.0    # composite, 0–1; 1 = strong persistent trend


@dataclass
class ChopMetrics:
    """Choppiness / range-bound features."""
    choppiness_index:   float = 0.0    # CCI-inspired; > 0.618 = choppy, < 0.382 = trending
    range_compression:  float = 0.0    # recent ATR / older ATR; < 1 = compressed
    body_quality_mean:  float = 0.0    # mean body-to-range ratio over window; low = choppy
    reversal_count:     int   = 0      # direction reversals in the source window


# ── Regime snapshot (persistence record) ─────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """Full persisted regime classification record.

    One record per meaningful regime change for a given
    (asset, timeframe) pair.
    """
    # Identity
    regime_id:       str               # UUID string generated at classification time
    created_at:      datetime          # When this snapshot was calculated

    # Context
    asset:           str
    asset_class:     str               # e.g. "crypto", "equity", "forex"
    timeframe:       str
    strategy_mode:   str               # "SCALP" | "INTERMEDIATE" | "SWING" | "UNKNOWN"

    # Classification result
    regime_label:    RegimeLabel
    confidence_score: float            # 0.0 – 1.0

    # Evidence (human-readable + machine metrics)
    evidence_summary:       str        # plain English rationale
    volatility_metrics:     VolatilityMetrics  = field(default_factory=VolatilityMetrics)
    trend_metrics:          TrendMetrics       = field(default_factory=TrendMetrics)
    chop_metrics:           ChopMetrics        = field(default_factory=ChopMetrics)

    # Optional / external
    news_instability_flag:  bool       = False   # from external feed if available
    news_source:            Optional[str] = None  # "external" | None
    source_window:          int        = 50       # candles used for classification


# ── Lightweight pipeline carrier ─────────────────────────────────────────────

@dataclass
class RegimeContext:
    """Compact regime state threaded through the signal/risk pipeline.

    Populated by the regime engine before signal evaluation.
    All fields are optional — presence of None means regime is unknown.
    The pipeline must treat None as fail-open (no modifier applied).
    """
    regime_label:       Optional[RegimeLabel] = None
    confidence_score:   float                 = 0.0
    evidence_summary:   str                   = ""

    # Soft modifier fields (populated by regime_gating; all default to 1.0 = no change)
    ml_threshold_delta:    float = 0.0   # added to base ML threshold; 0.0 = no change
    ai_threshold_delta:    float = 0.0   # added to base AI threshold
    position_size_factor:  float = 1.0   # multiplied against base size; 1.0 = no change
    score_bias:            float = 0.0   # added to raw score after computation

    # Observability
    snapshot_id:        Optional[str] = None   # links to regime_snapshots.regime_id
    news_input_present: bool          = False   # was news input available this cycle

    # ── Phase 12: enriched context for adaptation & transition tracking ───────
    previous_label:         Optional[RegimeLabel] = None   # label from prior cycle (None = first)
    regime_duration_seconds: float                = 0.0    # seconds since last regime change
    timestamp:              Optional[datetime]    = None   # when this context was computed
    asset:                  str                   = ""     # symbol this context applies to
    timeframe:              str                   = ""     # timeframe this context applies to

    # Phase 12: additive score adjustment applied by regime adapter
    regime_score_adjustment: float               = 0.0    # added to score_total after compute_score
    regime_score_reason:     str                  = ""     # human-readable reason for adjustment
    # Phase 12: entry filter result
    regime_entry_allowed:    bool                 = True   # False = entry rejected by regime filter
    regime_entry_reason:     str                  = ""     # rejection/acceptance reason

    def is_confident(self, min_confidence: float = 0.40) -> bool:
        """True when we have a non-unknown label with enough confidence."""
        return (
            self.regime_label is not None
            and self.regime_label is not RegimeLabel.UNKNOWN
            and self.confidence_score >= min_confidence
        )

    def is_adverse(self) -> bool:
        """True for regimes with reduced entry trust."""
        return (
            self.regime_label is not None
            and self.regime_label.is_adverse()
            and self.is_confident()
        )

    def to_log_str(self) -> str:
        label = self.regime_label.value if self.regime_label else "UNKNOWN"
        prev = self.previous_label.value if self.previous_label else "none"
        return (
            f"regime={label} conf={self.confidence_score:.2f} "
            f"prev={prev} dur={self.regime_duration_seconds:.0f}s "
            f"news={'yes' if self.news_input_present else 'no'}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_label":       self.regime_label.value if self.regime_label else "UNKNOWN",
            "confidence_score":   round(self.confidence_score, 4),
            "evidence_summary":   self.evidence_summary,
            "ml_threshold_delta": self.ml_threshold_delta,
            "ai_threshold_delta": self.ai_threshold_delta,
            "position_size_factor": self.position_size_factor,
            "score_bias":         self.score_bias,
            "snapshot_id":        self.snapshot_id,
            "news_input_present": self.news_input_present,
            "previous_label":     self.previous_label.value if self.previous_label else None,
            "regime_duration_seconds": self.regime_duration_seconds,
            "timestamp":          self.timestamp.isoformat() if self.timestamp else None,
            "asset":              self.asset,
            "timeframe":          self.timeframe,
            "regime_score_adjustment": self.regime_score_adjustment,
            "regime_score_reason":     self.regime_score_reason,
            "regime_entry_allowed":    self.regime_entry_allowed,
            "regime_entry_reason":     self.regime_entry_reason,
        }

    def macro_labels(self) -> FrozenSet[MacroRegime]:
        """Return macro regime facets for the current label."""
        if self.regime_label is None:
            return frozenset({MacroRegime.UNCERTAIN})
        return self.regime_label.macro_labels()
