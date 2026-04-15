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
from typing import Dict, List, Optional, Tuple, Literal

from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord
from src.risk.position_sizer import calculate_position_size
from src.risk.trailing_stop import TrailingStop

logger = logging.getLogger(__name__)

# Timeframes ≤ 30 minutes are "small" — they get priority over large-TF trades.
_SMALL_TIMEFRAMES = {"1m", "2m", "3m", "5m", "15m", "30m"}


def _is_small_tf(tf: str) -> bool:
    return (tf or "").lower() in _SMALL_TIMEFRAMES


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

        # ── Pyramid state ─────────────────────────────────────────────────────
        # Tracks how many pyramid adds have been made per asset (asset → count).
        # Conservative: first add at +1 % profit, second at +2 %, max 2 total.
        self._pyramid_counts:     Dict[str, int]       = {}
        self.max_pyramids:        int                  = 2
        self.pyramid_profit_thresholds: List[float]   = [1.0, 2.0]  # % unrealised

        # ── Cross-TF confirmation cap ─────────────────────────────────────────
        # Maximum number of simultaneous same-direction positions across
        # different timeframes for the same asset.
        self.max_cross_tf_confirmations: int = 4

        # ── Re-entry cooldown ─────────────────────────────────────────────────
        # After a position closes on an asset+direction, block new entries
        # for this many seconds to prevent scan-cycle retriggers.
        # "{asset}:{BUY|SELL}" → datetime of last close.
        self._recent_closes: Dict[str, datetime] = {}
        self.reentry_cooldown_s: float = 300.0   # 5 minutes

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

        # 3. Duplicate position on same asset (this scanner / same TF).
        #    Small TF (≤30m): opposing direction allowed — each trade is
        #    independent and managed by its own stop/TP.
        #    Large TF (>30m): opposing direction → REJECT.
        #    Same direction → pyramid check.
        asset = signal.asset
        has_same_tf_conflict = False
        for rec in self.open_positions.values():
            if rec.asset != asset:
                continue
            if signal.signal_type != rec.signal_type:
                if _is_small_tf(signal.timeframe):
                    continue   # small TF: allow opposing, skip this record
                return False, (
                    f"DIRECTION_CONFLICT ({asset}: existing={rec.signal_type} "
                    f"new={signal.signal_type} — large TF blocked)"
                )
            # Same direction — check pyramid
            pyramid_reason = self._check_pyramid(signal, rec)
            if pyramid_reason is not None:
                return pyramid_reason
            has_same_tf_conflict = True

        if has_same_tf_conflict:
            return False, f"DUPLICATE_POSITION ({asset} already open)"

        # 3.5 Post-close cooldown: block re-entries on same asset+direction
        #     within cooldown window (prevents scan-cycle retriggers).
        cooldown_key = f"{asset}:{signal.signal_type}"
        last_close = self._recent_closes.get(cooldown_key)
        if last_close is not None:
            elapsed = (datetime.now() - last_close).total_seconds()
            if elapsed < self.reentry_cooldown_s:
                remaining = int(self.reentry_cooldown_s - elapsed)
                return False, f"REENTRY_COOLDOWN ({asset} {signal.signal_type}: {remaining}s left)"

        # 4. Cross-timeframe guard (SQLite — OTHER scanner instances).
        #
        #    Small TF (≤30m) = ≤30 min candles.  Large TF = 1h+.
        #
        #    SAME direction on any other TF → ALLOW (cross-TF confirmation).
        #    OPPOSITE direction:
        #      new=small, existing=large → ALLOW  (small wins, runs independently)
        #      new=small, existing=small → ALLOW  (both short-term, both allowed)
        #      new=large, existing=small → BLOCK  (small has priority)
        #      new=large, existing=large → BLOCK  (no opposing long-running trades)
        try:
            from src.data.db import get_open_trades
            db_open = get_open_trades()
            same_dir_count = 0
            new_tf       = (signal.timeframe or "").lower()
            new_is_small = _is_small_tf(new_tf)
            for row in db_open:
                if row.get("asset") != asset or row.get("status") != "OPEN":
                    continue
                db_tf = (row.get("timeframe") or "").lower()
                if db_tf == new_tf:
                    continue   # same TF handled in step 3
                db_direction = row.get("signal_type", "")
                db_is_small  = _is_small_tf(db_tf)

                if db_direction == signal.signal_type:
                    same_dir_count += 1
                    continue

                # Opposing direction — apply small-wins-large rule
                if new_is_small:
                    # Small TF always wins: allow regardless of existing TF size
                    logger.info(
                        "Cross-TF: small %s %s allowed despite opposing %s %s",
                        new_tf, signal.signal_type, db_tf, db_direction,
                    )
                    continue
                else:
                    # New signal is large TF — block it
                    return False, (
                        f"CROSS_TF_DIRECTION_CONFLICT ({asset}: "
                        f"{new_tf} {signal.signal_type} vs {db_tf} {db_direction})"
                    )

            if same_dir_count >= self.max_cross_tf_confirmations:
                return False, (
                    f"CROSS_TF_CONFIRMATION_CAP ({asset}: "
                    f"{same_dir_count}/{self.max_cross_tf_confirmations} TFs already open)"
                )
            if same_dir_count > 0:
                logger.info(
                    "Cross-TF confirmation: %s %s approved (%d same-direction TFs open)",
                    asset, signal.signal_type, same_dir_count,
                )
                return True, f"APPROVED_CROSS_TF_CONFIRM ({same_dir_count + 1} TFs)"
        except Exception as _db_err:
            logger.debug("Cross-TF SQLite check failed: %s", _db_err)

        return True, "APPROVED"

    def _check_pyramid(
        self,
        signal: "BuySignalResult | SellSignalResult",
        existing: "TradeRecord",
    ) -> Optional[Tuple[bool, str]]:
        """Determine whether a pyramid add is allowed on an existing position.

        Returns (True, "APPROVED_PYRAMID") if allowed, (False, reason) if not,
        or None if the caller should fall through to the standard DUPLICATE check.
        """
        asset = signal.asset

        # Must be same direction
        if signal.signal_type != existing.signal_type:
            return False, f"DIRECTION_CONFLICT ({asset}: existing={existing.signal_type} new={signal.signal_type})"

        # Pyramid count cap
        current_count = self._pyramid_counts.get(asset, 0)
        if current_count >= self.max_pyramids:
            return False, f"PYRAMID_CAP ({asset}: {current_count}/{self.max_pyramids} adds used)"

        # Existing position must be profitable enough for the next level
        required_pct = self.pyramid_profit_thresholds[current_count]  # 1.0 % or 2.0 %
        if existing.entry_price <= 0:
            return None
        if existing.signal_type == "BUY":
            unrealised = (signal.entry_price - existing.entry_price) / existing.entry_price * 100.0
        else:
            unrealised = (existing.entry_price - signal.entry_price) / existing.entry_price * 100.0

        if unrealised < required_pct:
            return False, (
                f"PYRAMID_NOT_PROFITABLE ({asset}: need +{required_pct}% profit, "
                f"current={unrealised:.2f}%)"
            )

        return True, "APPROVED_PYRAMID"

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

    def record_pyramid(self, asset: str) -> None:
        """Increment the pyramid counter for an asset after a pyramid add is placed."""
        self._pyramid_counts[asset] = self._pyramid_counts.get(asset, 0) + 1

    def pyramid_size_factor(self, asset: str) -> float:
        """Return the position-size multiplier for the next pyramid add (50 % each time)."""
        return 0.50

    def reset_pyramid(self, asset: str) -> None:
        """Clear pyramid state when a position is fully closed."""
        self._pyramid_counts.pop(asset, None)

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
        """Check whether an open trade should be closed on this bar's close.

        Exit priority order (checked top to bottom; first match wins):
          1. HARD_STOP          — price hit the fixed 2 % safety stop
          2. PEAK_GIVEBACK_EXIT — price retraced giveback_frac of max favorable
                                  move from entry (evaluated on bar close).
                                  NOTE: this can fire at a loss when the
                                  favorable excursion was small.  That is
                                  expected behaviour — see PeakGiveback docs.
          3. TRAIL_STOP         — teeth-based trailing stop was breached
          4. ALLIGATOR_TP       — lips crossed back through teeth (momentum end)

        ``peak_giveback`` is optional :class:`PeakGiveback` (must be updated
        each bar via ``update_bar`` before calling this method).
        """
        rec = self.open_positions.get(trade_id)
        if rec is None:
            return False, ""

        trail: Optional[TrailingStop] = getattr(rec, "_trail_stop", None)

        # 1. Hard stop (safety net first)
        if rec.signal_type == "BUY" and current_price <= rec.stop_loss_hard:
            return True, "HARD_STOP"
        if rec.signal_type == "SELL" and current_price >= rec.stop_loss_hard:
            return True, "HARD_STOP"

        # 2. Teeth trailing stop (only moves forward, never backward)
        if trail and trail.is_triggered(current_price):
            return True, "TRAIL_STOP"

        # 3. Peak giveback — bar-close retraced giveback_frac of max favorable move.
        #    Checked AFTER trailing stop so the forward-only trail gets priority.
        if peak_giveback is not None and peak_giveback.is_triggered(current_price):  # type: ignore[union-attr]
            return True, "PEAK_GIVEBACK_EXIT"

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
        self._recent_closes[f"{rec.asset}:{rec.signal_type}"] = datetime.now()

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
        rec = self.open_positions.pop(trade_id, None)
        self.daily_realised_pnl += pnl
        self.account_balance    += pnl
        # Reset pyramid counter only when NO more open positions remain for that asset
        if rec is not None:
            asset = rec.asset
            still_open = any(r.asset == asset for r in self.open_positions.values())
            if not still_open:
                self.reset_pyramid(asset)
            # Stamp cooldown so the next scan cycle can't immediately re-enter
            # the same asset+direction.
            self._recent_closes[f"{asset}:{rec.signal_type}"] = datetime.now()

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
