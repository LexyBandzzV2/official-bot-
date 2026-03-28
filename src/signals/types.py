"""Shared dataclasses for signal results and trade records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
    close_reason:     Optional[str]      = None  # ALLIGATOR_TP|TRAILING_TP|TRAIL_STOP|HARD_STOP|MANUAL
    pnl:              float = 0.0
    pnl_pct:          float = 0.0
    max_trail_reached:float = 0.0
    status:           str   = "OPEN"   # "OPEN" | "CLOSED"
