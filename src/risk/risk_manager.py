"""Risk Manager — enforces all trading rules before any order is placed.

Rules enforced (in check order):
    1. Daily kill switch  : if daily drawdown ≥ 10 %, block all new signals
    2. Hourly trade limit : max 15 trades per clock hour
    3. Duplicate guard    : same asset cannot have two open positions

All rule violations are logged with a reason code so every rejection
appears in the trade log for research.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord
from src.risk.position_sizer import calculate_position_size
from src.risk.trailing_stop import TrailingStop

logger = logging.getLogger(__name__)


class RiskManager:
    """Central gatekeeper — every signal must pass through approve_signal()
    before being sent to the execution layer."""

    def __init__(
        self,
        account_balance:     float = 10_000.0,
        max_risk_per_trade:  float = 0.01,   # 1 %
        stop_loss_pct:       float = 0.02,   # 2 %
        max_daily_drawdown:  float = 0.10,   # 10 %
        max_trades_per_hour: int   = 15,
    ) -> None:

        self._bal_at_open         = account_balance
        self.account_balance      = account_balance
        self.max_risk_per_trade   = max_risk_per_trade
        self.stop_loss_pct        = stop_loss_pct
        self.max_daily_drawdown   = max_daily_drawdown
        self.max_trades_per_hour  = max_trades_per_hour

        # ── State ─────────────────────────────────────────────────────────────
        self.daily_start_balance: float                = account_balance
        self.daily_realised_pnl:  float                = 0.0
        self.daily_kill_active:   bool                 = False
        self.last_day_reset:      datetime             = _today_midnight()

        self._hourly_count:       int                  = 0
        self._hour_window_start:  datetime             = _current_hour()

        self.open_positions:      Dict[str, TradeRecord] = {}   # trade_id → record

    # ── Public API ────────────────────────────────────────────────────────────

    def approve_signal(
        self,
        signal: BuySignalResult | SellSignalResult,
    ) -> Tuple[bool, str]:
        """Return (approved: bool, reason: str).

        On approval the caller should immediately call record_opened().
        """
        self._refresh_daily()
        self._refresh_hourly()

        # 1. Daily kill switch
        if self.daily_kill_active:
            return False, "DAILY_KILL_SWITCH_ACTIVE"

        daily_loss_pct = self._daily_loss_pct()
        if daily_loss_pct >= self.max_daily_drawdown:
            self.daily_kill_active = True
            logger.critical(
                "DAILY KILL SWITCH: loss %.2f%% hit limit %.0f%%",
                daily_loss_pct * 100, self.max_daily_drawdown * 100,
            )
            return False, f"DAILY_LIMIT_HIT ({daily_loss_pct*100:.2f}%)"

        # 2. Hourly trade cap
        if self._hourly_count >= self.max_trades_per_hour:
            return False, (
                f"HOURLY_LIMIT_EXCEEDED "
                f"({self._hourly_count}/{self.max_trades_per_hour} trades this hour)"
            )

        # 3. Duplicate position on same asset
        asset = signal.asset
        for rec in self.open_positions.values():
            if rec.asset == asset:
                return False, f"DUPLICATE_POSITION ({asset} already open)"

        return True, "APPROVED"

    def record_opened(
        self,
        trade_id:   str,
        signal:     BuySignalResult | SellSignalResult,
        fill_price: float,
    ) -> TradeRecord:
        """Create a TradeRecord, attach a TrailingStop, increment counters."""

        pos_size = calculate_position_size(
            account_balance  = self.account_balance,
            entry_price      = fill_price,
            stop_loss_price  = (fill_price * (1.0 - self.stop_loss_pct)
                                if signal.signal_type == "BUY"
                                else fill_price * (1.0 + self.stop_loss_pct)),
            risk_pct         = self.max_risk_per_trade,
        )

        stop_hard = (fill_price * (1.0 - self.stop_loss_pct)
                     if signal.signal_type == "BUY"
                     else fill_price * (1.0 + self.stop_loss_pct))

        trail = TrailingStop(
            direction     = signal.signal_type.lower(),
            entry_price   = fill_price,
            initial_teeth = signal.teeth_price,
            stop_loss_pct = self.stop_loss_pct,
        )

        rec = TradeRecord(
            trade_id         = trade_id,
            signal_type      = signal.signal_type,
            asset            = signal.asset,
            timeframe        = signal.timeframe,
            entry_time       = datetime.now(),
            entry_price      = fill_price,
            stop_loss_hard   = stop_hard,
            trailing_stop    = trail.current_stop,
            position_size    = pos_size,
            account_risk_pct = self.max_risk_per_trade * 100,
            alligator_point  = signal.alligator_point,
            stochastic_point = signal.stochastic_point,
            vortex_point     = signal.vortex_point,
            jaw_at_entry     = signal.jaw_price,
            teeth_at_entry   = signal.teeth_price,
            lips_at_entry    = signal.lips_price,
            ml_confidence    = signal.ml_confidence,
            ai_confidence    = signal.ai_confidence,
            max_trail_reached= trail.current_stop,
        )

        # Attach the live trailing stop object directly to the record
        rec._trail_stop = trail  # type: ignore[attr-defined]

        self.open_positions[trade_id] = rec
        self._hourly_count += 1
        return rec

    def update_trail(self, trade_id: str, teeth_price: float) -> Optional[float]:
        """Ratchet the trailing stop for an open trade.

        Returns the new stop level, or None if trade not found.
        """
        rec = self.open_positions.get(trade_id)
        if rec is None:
            return None

        trail = getattr(rec, "_trail_stop", None)
        if trail is None:
            return None

        new_stop = trail.update(teeth_price)
        rec.trailing_stop    = new_stop
        rec.max_trail_reached = trail.max_trail
        return new_stop

    def check_exit_conditions(
        self,
        trade_id:      str,
        current_price: float,
        ha_df=None,
        peak_giveback: Optional[object] = None,
    ) -> Tuple[bool, str]:
        """Check whether an open trade should be closed.

        Order: HARD_STOP → TRAILING_TP (peak giveback) → TRAIL_STOP → ALLIGATOR_TP.

        ``peak_giveback`` is optional :class:`PeakGiveback` (must be updated each bar).
        """
        rec = self.open_positions.get(trade_id)
        if rec is None:
            return False, ""

        trail: TrailingStop = getattr(rec, "_trail_stop", None)

        # 1. Hard stop (safety net first)
        if rec.signal_type == "BUY" and current_price <= rec.stop_loss_hard:
            return True, "HARD_STOP"
        if rec.signal_type == "SELL" and current_price >= rec.stop_loss_hard:
            return True, "HARD_STOP"

        # 2. Peak giveback (trailing take-profit style)
        if peak_giveback is not None and peak_giveback.is_triggered(current_price):
            return True, "TRAILING_TP"

        # 3. Teeth trailing stop
        if trail and trail.is_triggered(current_price):
            return True, "TRAIL_STOP"

        # 4. Alligator lips-touch (fallback / audit exit)
        if ha_df is not None:
            from src.indicators.alligator import (
                check_lips_touch_teeth_down,
                check_lips_touch_teeth_up,
            )
            if rec.signal_type == "BUY" and check_lips_touch_teeth_down(ha_df):
                return True, "ALLIGATOR_TP"
            if rec.signal_type == "SELL" and check_lips_touch_teeth_up(ha_df):
                return True, "ALLIGATOR_TP"

        return False, ""

    def record_closed(
        self,
        trade_id:    str,
        exit_price:  float,
        close_reason:str,
        exit_time:   Optional[datetime] = None,
    ) -> Optional[TradeRecord]:
        """Mark a trade as closed, calculate PnL, and update balance."""
        rec = self.open_positions.pop(trade_id, None)
        if rec is None:
            return None

        exit_time = exit_time or datetime.now()
        rec.exit_time    = exit_time
        rec.exit_price   = exit_price
        rec.close_reason = close_reason
        rec.status       = "CLOSED"

        if rec.signal_type == "BUY":
            rec.pnl = (exit_price - rec.entry_price) * rec.position_size
        else:
            rec.pnl = (rec.entry_price - exit_price) * rec.position_size

        rec.pnl_pct = (rec.pnl / (rec.entry_price * rec.position_size)) * 100

        self.daily_realised_pnl += rec.pnl
        self.account_balance    += rec.pnl

        return rec

    # ── Queries ───────────────────────────────────────────────────────────────

    def daily_loss_pct_display(self) -> float:
        return self._daily_loss_pct() * 100

    def remaining_hourly_trades(self) -> int:
        self._refresh_hourly()
        return max(0, self.max_trades_per_hour - self._hourly_count)

    def open_count(self) -> int:
        return len(self.open_positions)

    def is_kill_switch_active(self) -> bool:
        """Return True if the daily kill switch has been triggered."""
        self._refresh_daily()
        if self._daily_loss_pct() >= self.max_daily_drawdown:
            self.daily_kill_active = True
        return self.daily_kill_active

    def can_open_trade(self) -> bool:
        """Return True if a new trade is allowed right now."""
        self._refresh_daily()
        self._refresh_hourly()
        return not self.daily_kill_active and self._hourly_count < self.max_trades_per_hour

    # ── Convenience overloads ─────────────────────────────────────────────────

    def record_opened_from_record(self, rec: "TradeRecord") -> None:
        """Register an already-built TradeRecord (used by scanner)."""
        self.open_positions[rec.trade_id] = rec
        self._hourly_count += 1

    def record_closed_pnl(self, trade_id: str, pnl: float) -> None:
        """Simpler close path: scanner already computed PnL, just update counters."""
        self.open_positions.pop(trade_id, None)
        self.daily_realised_pnl += pnl
        self.account_balance    += pnl

    # ── Internals ─────────────────────────────────────────────────────────────

    def _daily_loss_pct(self) -> float:
        if self.daily_start_balance == 0:
            return 0.0
        loss = -self.daily_realised_pnl  # positive = loss
        return max(0.0, loss / self.daily_start_balance)

    def _refresh_daily(self) -> None:
        today = _today_midnight()
        if today > self.last_day_reset:
            self.daily_start_balance = self.account_balance
            self.daily_realised_pnl  = 0.0
            self.daily_kill_active   = False
            self.last_day_reset      = today
            logger.info("Daily counters reset — new session started.")

    def _refresh_hourly(self) -> None:
        current = _current_hour()
        if current > self._hour_window_start:
            self._hourly_count      = 0
            self._hour_window_start = current


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today_midnight() -> datetime:
    n = datetime.now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _current_hour() -> datetime:
    n = datetime.now()
    return n.replace(minute=0, second=0, microsecond=0)
