"""Historical backtester — replays OHLCV data and simulates the full signal pipeline.

Candle-by-candle walkthrough:
  1. Load raw OHLCV for asset/timeframe/date range
  2. Convert entire buffer to Heikin Ashi ONCE (never re-convert)
  3. For each bar (from bar 60 onward, to warm up indicators):
       a. Slice window up to current bar
       b. Run SignalEngine on the HA window
       c. If valid signal + risk approved → open simulated trade
       d. On all subsequent bars, update trailing stop + check exit conditions
  4. At end, return list[TradeRecord] with full entry/exit details

Historical data comes from market_data.get_historical_ohlcv().
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

try:
    from src.data.heikin_ashi   import convert_to_heikin_ashi
    from src.data.market_data   import get_historical_ohlcv
    from src.signals.signal_engine import SignalEngine
    from src.risk.position_sizer   import calculate_position_size
    from src.risk.trailing_stop    import TrailingStop
    from src.indicators.alligator  import calculate_alligator, check_lips_touch_teeth_down, check_lips_touch_teeth_up
    from src.signals.types         import TradeRecord
    from src.config                import (
        MAX_RISK_PER_TRADE, STOP_LOSS_PCT, MAX_DAILY_DRAWDOWN, MAX_TRADES_PER_HOUR,
        ML_CONFIDENCE_THRESHOLD, TRAILING_TP_ENABLED, TRAILING_TP_GIVEBACK,
    )
    from src.risk.trailing_take_profit import PeakGiveback
except ImportError as e:
    log.error("Backtest import error: %s", e)
    raise

WARMUP_BARS = 60   # minimum bars needed before emitting signals


class Backtester:
    """Simulates the full trading pipeline on historical data."""

    def __init__(
        self,
        account_balance: float   = 10_000.0,
        max_risk_pct:    float   = MAX_RISK_PER_TRADE,
        stop_loss_pct:   float   = STOP_LOSS_PCT,
        max_daily_dd:    float   = MAX_DAILY_DRAWDOWN,
        max_trades_hr:   int     = MAX_TRADES_PER_HOUR,
        use_ml:          bool    = False,   # False until model is trained
        use_ai:          bool    = False,   # False for speed in backtest
    ) -> None:
        self.account_balance = account_balance
        self.max_risk_pct    = max_risk_pct
        self.stop_loss_pct   = stop_loss_pct
        self.max_daily_dd    = max_daily_dd
        self.max_trades_hr   = max_trades_hr
        self.use_ml          = use_ml
        self.use_ai          = use_ai

    def run(
        self,
        symbol:    str,
        timeframe: str,
        start:     datetime,
        end:       Optional[datetime] = None,
        source:    Optional[str]      = None,
    ) -> list[TradeRecord]:
        """Run backtest. Returns list of all completed TradeRecord objects."""
        log.info("Backtest: fetching %s %s from %s", symbol, timeframe, start.date())
        raw_df = get_historical_ohlcv(symbol, timeframe, start=start, end=end, source=source)
        if raw_df.empty:
            log.error("No data for %s %s", symbol, timeframe)
            return []

        # Convert ALL raw candles to HA once
        ha_df = convert_to_heikin_ashi(raw_df)
        log.info("Backtest: %d HA candles loaded for %s %s", len(ha_df), symbol, timeframe)

        closed_trades: list[TradeRecord]      = []
        open_positions: list[tuple[TradeRecord, TrailingStop, Optional[PeakGiveback]]] = []

        balance      = self.account_balance
        daily_start  = self.account_balance
        daily_pnl    = 0.0
        trades_hour  = 0
        kill_switch  = False
        current_hour = -1
        current_day  = None

        engine = SignalEngine(symbol, timeframe)

        for bar_idx in range(WARMUP_BARS, len(ha_df)):
            current_bar = ha_df.iloc[bar_idx]
            bar_time    = current_bar.get("time", pd.Timestamp.now())

            # ── Daily reset ──────────────────────────────────────────────────
            bar_date = bar_time.date() if hasattr(bar_time, "date") else None
            if bar_date and bar_date != current_day:
                current_day = bar_date
                daily_start = balance
                daily_pnl   = 0.0
                kill_switch = False
                trades_hour = 0

            # ── Hourly reset ─────────────────────────────────────────────────
            try:
                bar_hour = bar_time.hour
            except AttributeError:
                bar_hour = 0
            if bar_hour != current_hour:
                current_hour = bar_hour
                trades_hour  = 0

            # ── Process open positions first ─────────────────────────────────
            ag_full = calculate_alligator(ha_df.iloc[: bar_idx + 1].copy())
            still_open: list[tuple[TradeRecord, TrailingStop, Optional[PeakGiveback]]] = []
            for rec, trail, tp_track in open_positions:
                last_ag = ag_full.iloc[-1]
                try:
                    teeth_now = float(last_ag["teeth"])
                    if np.isnan(teeth_now):
                        teeth_now = None
                except Exception:
                    teeth_now = None

                if teeth_now is not None:
                    new_stop = trail.update(teeth_now)
                    rec.trailing_stop = new_stop
                    rec.max_trail_reached = max(
                        rec.max_trail_reached, abs(new_stop - rec.entry_price)
                    )

                if tp_track is not None:
                    tp_track.update_bar(float(last_ag["high"]), float(last_ag["low"]))

                close_price = float(current_bar["ha_close"])
                close_reason: Optional[str] = None

                # Order matches risk_manager.check_exit_conditions:
                # HARD_STOP → TRAILING_TP → TRAIL_STOP → ALLIGATOR_TP
                if rec.signal_type == "BUY" and close_price <= rec.stop_loss_hard:
                    close_reason = "HARD_STOP"
                elif rec.signal_type == "SELL" and close_price >= rec.stop_loss_hard:
                    close_reason = "HARD_STOP"
                elif tp_track is not None and tp_track.is_triggered(close_price):
                    close_reason = "TRAILING_TP"
                elif trail.is_triggered(close_price):
                    close_reason = "TRAIL_STOP"
                elif len(ag_full) >= 2:
                    if rec.signal_type == "BUY" and check_lips_touch_teeth_down(ag_full):
                        close_reason = "ALLIGATOR_TP"
                    elif rec.signal_type == "SELL" and check_lips_touch_teeth_up(ag_full):
                        close_reason = "ALLIGATOR_TP"

                if close_reason:
                    # Close the trade
                    close_time = bar_time if hasattr(bar_time, "tzinfo") else datetime.now(timezone.utc)
                    if rec.signal_type == "BUY":
                        raw_pnl_pct = (close_price - rec.entry_price) / rec.entry_price * 100
                    else:
                        raw_pnl_pct = (rec.entry_price - close_price) / rec.entry_price * 100

                    raw_pnl = raw_pnl_pct / 100 * rec.position_size * rec.entry_price

                    rec.exit_time    = close_time
                    rec.exit_price   = close_price
                    rec.close_reason = close_reason
                    rec.pnl          = raw_pnl
                    rec.pnl_pct      = raw_pnl_pct
                    rec.status       = "CLOSED"

                    balance   += raw_pnl
                    daily_pnl += raw_pnl
                    closed_trades.append(rec)

                    # Daily drawdown check
                    if daily_start > 0 and -(daily_pnl / daily_start) >= self.max_daily_dd:
                        kill_switch = True
                else:
                    still_open.append((rec, trail, tp_track))

            open_positions = still_open

            # ── Check kill switch ─────────────────────────────────────────────
            if kill_switch:
                continue

            # ── Check hourly cap ─────────────────────────────────────────────
            if trades_hour >= self.max_trades_hr:
                continue

            # ── Signal detection on HA window ────────────────────────────────
            window   = ha_df.iloc[max(0, bar_idx - 199):bar_idx + 1]
            result   = engine.evaluate_ha(window)

            buy_sig  = result.get("buy")
            sell_sig = result.get("sell")

            for sig in [buy_sig, sell_sig]:
                if sig is None or not sig.is_valid:
                    continue

                # ML filter (if model available and enabled)
                if self.use_ml:
                    try:
                        from src.ml.model import passes_ml_filter
                        passed, prob = passes_ml_filter(sig)
                        sig.ml_confidence = prob
                        if not passed:
                            continue
                    except Exception:
                        pass

                # Position size for 1% account risk
                entry  = sig.entry_price
                sl     = sig.stop_loss
                size   = calculate_position_size(balance, entry, sl, self.max_risk_pct)

                if size <= 0:
                    continue

                ag_at_entry = calculate_alligator(ha_df.iloc[: bar_idx + 1].copy())
                try:
                    teeth_now = float(ag_at_entry.iloc[-1]["teeth"])
                    if np.isnan(teeth_now):
                        teeth_now = entry
                except Exception:
                    teeth_now = entry

                trail = TrailingStop(
                    direction    = "buy" if sig.signal_type == "BUY" else "sell",
                    entry_price  = entry,
                    initial_teeth= teeth_now,
                    stop_loss_pct= self.stop_loss_pct,
                )

                tp_track: Optional[PeakGiveback] = None
                if TRAILING_TP_ENABLED:
                    tp_track = PeakGiveback(
                        direction="buy" if sig.signal_type == "BUY" else "sell",
                        entry_price=entry,
                        giveback_frac=TRAILING_TP_GIVEBACK,
                    )
                    tp_track.update_bar(
                        float(current_bar["high"]),
                        float(current_bar["low"]),
                    )

                rec = TradeRecord(
                    trade_id        = str(uuid.uuid4()),
                    signal_type     = sig.signal_type,
                    asset           = symbol,
                    timeframe       = timeframe,
                    entry_time      = bar_time if hasattr(bar_time, "tzinfo") else datetime.now(timezone.utc),
                    entry_price     = entry,
                    stop_loss_hard  = sl,
                    trailing_stop   = trail.current_stop,
                    position_size   = size,
                    account_risk_pct= self.max_risk_pct,
                    alligator_point = sig.alligator_point,
                    stochastic_point= sig.stochastic_point,
                    vortex_point    = sig.vortex_point,
                    jaw_at_entry    = sig.jaw_price,
                    teeth_at_entry  = sig.teeth_price,
                    lips_at_entry   = sig.lips_price,
                    ml_confidence   = sig.ml_confidence,
                    max_trail_reached=0.0,
                )
                rec._trail_stop = trail  # type: ignore[attr-defined]
                open_positions.append((rec, trail, tp_track))
                trades_hour += 1

        # Mark any remaining open positions as still-open (no forced close at backtest end)
        log.info(
            "Backtest complete: %d closed trades, %d still open at EOD",
            len(closed_trades), len(open_positions),
        )
        return closed_trades
