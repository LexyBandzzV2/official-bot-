"""Shared dataclasses for signal results and trade records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.signals.strategy_mode import StrategyMode


@dataclass
class BuySignalResult:
    """Result returned by BuySignalWorker.evaluate()."""
    signal_type:         str      = "BUY"
    is_valid:            bool     = False
    points:              int      = 0
    max_points:          int      = 3
    alligator_point:     bool     = False
    stochastic_point:    bool     = False
    vortex_point:        bool     = False
    entry_price:         float    = 0.0
    stop_loss:           float    = 0.0
    stop_loss_pct:       float    = 2.0
    profit_estimate_pct: float    = 0.0
    take_profit_trigger: str      = "lips_crosses_down_to_teeth"
    notification_message:str      = ""
    timestamp:           datetime = field(default_factory=datetime.now)
    asset:               str      = ""
    timeframe:           str      = ""
    jaw_price:           float    = 0.0
    teeth_price:         float    = 0.0
    lips_price:          float    = 0.0
    ml_confidence:       Optional[float] = None
    ai_confidence:       Optional[float] = None
    ml_filtered:         bool     = False
    rejection_reason:    str      = ""
    signals_in_history:  int      = 0  # buy completions in the evaluated DataFrame
    last_signal_time:    Optional[str] = None
    strategy_mode:       str      = "UNKNOWN"
    # ── Phase 5: score breakdown and entry intelligence ────────────────────────
    indicator_flags:             Optional[str]   = None
    # e.g. "alligator+stochastic+vortex"
    entry_reason_code:           Optional[str]   = None
    # partial "al+st+vo" in worker; scanner appends ":ml87:ai72"
    accepted_signal:             bool            = False
    # True only when all gates (ML + AI + risk) passed
    structure_points:            float           = 0.0
    # 20 if alligator aligned, else 0
    indicator_points:            float           = 0.0
    # 10 × (stochastic_point + vortex_point)
    timeframe_alignment_points:  float           = 0.0
    # 10 for formal TF (3m/5m/15m/1h/2h/4h), 5 for informal
    candle_quality_points:       float           = 0.0
    # 0/10/20 based on last-bar body ratio from candle_quality module
    volatility_points:           float           = 0.0
    # 10 when ATR > 0, 0 when unavailable
    ml_adjustment_points:        float           = 0.0
    # +10 boosted, 0 passed, −20 vetoed (ML)
    score_total:                 float           = 0.0
    # sum of all sub-scores (0–100 scale)
    ml_effect:                   Optional[str]   = None
    # "vetoed" | "passed" | "boosted"
    ai_effect:                   Optional[str]   = None
    # "vetoed" | "passed" | "boosted"
    # ── Phase 11: regime context ──────────────────────────────────────────────
    regime_context:              Optional[object] = None
    # RegimeContext instance; None = regime unknown / not computed
    # ── Phase 14: live suitability audit ─────────────────────────────────────
    suitability_context:         Optional[object] = None
    # SuitabilityContext instance populated by the resolver
    suitability_rating:          Optional[str]    = None
    # HIGH | MEDIUM | LOW | BLOCKED | UNKNOWN
    suitability_score:           Optional[float]  = None
    suitability_reason:          str              = ""
    suitability_source_summary:  str              = ""
    skip_reason_code:            str              = ""
    decision_trace_json:         Optional[str]    = None
    active_profile_snapshot_id:  Optional[str]    = None
    live_activation_decision:    Optional[object] = None
    # LiveActivationDecision instance; None until resolver runs
    macro_regime:                Optional[str]    = None
    # MacroRegime.value string, copied from regime_context for easy DB storage
    regime_label:                Optional[str]    = None
    # RegimeLabel.value string, copied from regime_context for easy DB storage
    # ── Final Sprint: prefilter audit ────────────────────────────────────────
    prefilter_universe_group:    Optional[str]    = None
    prefilter_atr_pct:           Optional[float]  = None
    prefilter_volume_ratio:      Optional[float]  = None
    prefilter_rank_score:        Optional[float]  = None
    prefilter_passed:            Optional[bool]   = None
    prefilter_skip_reason:       str              = ""


@dataclass
class SellSignalResult:
    """Result returned by SellSignalWorker.evaluate()."""
    signal_type:         str      = "SELL"
    is_valid:            bool     = False
    points:              int      = 0
    max_points:          int      = 3
    alligator_point:     bool     = False
    stochastic_point:    bool     = False
    vortex_point:        bool     = False
    entry_price:         float    = 0.0
    stop_loss:           float    = 0.0
    stop_loss_pct:       float    = 2.0
    profit_estimate_pct: float    = 0.0
    take_profit_trigger: str      = "lips_crosses_up_to_teeth"
    notification_message:str      = ""
    timestamp:           datetime = field(default_factory=datetime.now)
    asset:               str      = ""
    timeframe:           str      = ""
    jaw_price:           float    = 0.0
    teeth_price:         float    = 0.0
    lips_price:          float    = 0.0
    ml_confidence:       Optional[float] = None
    ai_confidence:       Optional[float] = None
    ml_filtered:         bool     = False
    rejection_reason:    str      = ""
    signals_in_history:  int      = 0  # sell completions in the evaluated DataFrame
    last_signal_time:    Optional[str] = None
    strategy_mode:       str      = "UNKNOWN"
    # ── Phase 5: score breakdown and entry intelligence ────────────────────────
    indicator_flags:             Optional[str]   = None
    entry_reason_code:           Optional[str]   = None
    accepted_signal:             bool            = False
    structure_points:            float           = 0.0
    indicator_points:            float           = 0.0
    timeframe_alignment_points:  float           = 0.0
    candle_quality_points:       float           = 0.0
    volatility_points:           float           = 0.0
    ml_adjustment_points:        float           = 0.0
    score_total:                 float           = 0.0
    ml_effect:                   Optional[str]   = None
    ai_effect:                   Optional[str]   = None
    # ── Phase 11: regime context ──────────────────────────────────────────────
    regime_context:              Optional[object] = None
    # RegimeContext instance; None = regime unknown / not computed
    # ── Phase 14: live suitability audit ─────────────────────────────────────
    suitability_context:         Optional[object] = None
    suitability_rating:          Optional[str]    = None
    suitability_score:           Optional[float]  = None
    suitability_reason:          str              = ""
    suitability_source_summary:  str              = ""
    skip_reason_code:            str              = ""
    decision_trace_json:         Optional[str]    = None
    active_profile_snapshot_id:  Optional[str]    = None
    live_activation_decision:    Optional[object] = None
    macro_regime:                Optional[str]    = None
    regime_label:                Optional[str]    = None
    # ── Final Sprint: prefilter audit ────────────────────────────────────────
    prefilter_universe_group:    Optional[str]    = None
    prefilter_atr_pct:           Optional[float]  = None
    prefilter_volume_ratio:      Optional[float]  = None
    prefilter_rank_score:        Optional[float]  = None
    prefilter_passed:            Optional[bool]   = None
    prefilter_skip_reason:       str              = ""


@dataclass
class TradeRecord:
    """A live or closed trade tracked by the position manager."""
    trade_id:         str
    signal_type:      str       # 'BUY' or 'SELL'
    asset:            str
    timeframe:        str
    entry_time:       datetime
    entry_price:      float
    stop_loss_hard:   float     # the original 2 % hard floor
    trailing_stop:    float     # current trail level (updated each candle)
    position_size:    float
    account_risk_pct: float
    # indicator state at entry
    alligator_point:  bool
    stochastic_point: bool
    vortex_point:     bool
    jaw_at_entry:     float
    teeth_at_entry:   float
    lips_at_entry:    float
    # AI / ML
    ml_confidence:    Optional[float] = None
    ai_confidence:    Optional[float] = None
    # closed-trade fields (None while open)
    exit_time:        Optional[datetime] = None
    exit_price:       Optional[float]    = None
    close_reason:     Optional[str]      = None  # ALLIGATOR_TP|PEAK_GIVEBACK_EXIT|TRAIL_STOP|HARD_STOP|MANUAL
    # PEAK_GIVEBACK_EXIT: bar-close retraced giveback_frac of max favorable
    # move from entry.  May close at a loss when MFE was small.
    pnl:              float = 0.0
    pnl_pct:          float = 0.0
    max_trail_reached:float = 0.0
    status:           str   = "OPEN"   # "OPEN" | "CLOSED"
    strategy_mode:    str   = "UNKNOWN"
    # ── Phase 3: lifecycle observability fields ────────────────────────────────
    # Populated at open
    entry_reason:                     Optional[str]      = None
    # e.g. "alligator+stochastic+vortex | ml=87% ai=72%"
    initial_stop_value:               Optional[float]    = None
    initial_exit_policy:              Optional[str]      = None
    # e.g. "SCALP", "INTERMEDIATE", "SWING"
    # Updated per-bar during the trade
    max_unrealized_profit:            float              = 0.0   # MFE % (positive = gain)
    min_unrealized_profit:            float              = 0.0   # MAE % (negative = deepest loss)
    break_even_armed:                 bool               = False
    profit_lock_stage:                int                = 0     # 0=none, 1/2/3
    was_protected_profit:             bool               = False
    # True once break_even_armed OR profit_lock_stage >= 1
    trail_update_reason:              Optional[str]      = None
    # Last trail move reason (constrained; see save_lifecycle_event _VALID_TRAIL_REASONS)
    timestamp_of_mfe:                 Optional[datetime] = None
    timestamp_of_mae:                 Optional[datetime] = None
    protected_profit_activation_time: Optional[datetime] = None
    # Phase 4: richer observable state
    indicator_flags:                  Optional[str]      = None
    # e.g. "alligator+stochastic+vortex"
    entry_reason_code:                Optional[str]      = None
    # compact machine-readable: "al+st+vo:ml87:ai72"
    trail_active_mode:                Optional[str]      = None
    # "initial_stop"|"break_even"|"stage_1"|"stage_2"|"stage_3"|"candle_trail"|"atr_trail"
    used_fallback_policy:             bool               = False
    # True when timeframe is not in FORMAL_TIMEFRAMES (e.g. 1m, 30m, 3h, 1d)
    # Populated at close
    exit_policy_name:                 Optional[str]      = None
    # e.g. "SCALP_STAGE_2_LOCKED"
    # Phase 6: candle-strength fade observability
    fade_tighten_count:               int                = 0
    # incremented each time candle-trail tightening fires on this trade
    last_fade_body_ratio:             Optional[float]    = None
    # body_to_range_ratio of the bar when fade tightening last fired
    last_fade_wick_ratio:             Optional[float]    = None
    # adverse-wick fraction of the bar when fade tightening last fired
    # Phase 11: regime observability
    regime_label_at_entry:            Optional[str]      = None
    # RegimeLabel.value at trade open; for reporting and outcome correlation
    regime_confidence_at_entry:       float              = 0.0
    regime_snapshot_id:               Optional[str]      = None
    # links to regime_snapshots.regime_id for forensic queries
    # Phase 12: enriched regime observability at entry + exit
    regime_label_at_exit:             Optional[str]      = None
    regime_confidence_at_exit:        float              = 0.0
    regime_changed_during_trade:      bool               = False
    regime_transition_count:          int                = 0
    regime_score_adjustment:          float              = 0.0
    # additive score bias applied by regime adapter at entry
