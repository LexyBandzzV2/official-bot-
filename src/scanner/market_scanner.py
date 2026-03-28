"""Market scanner — continuous loop that polls live data and detects signals.

Scan cycle (runs on a schedule):
  1. Fetch latest N candles for every active symbol
  2. Convert raw OHLCV → Heikin Ashi
  3. Score candidates (candidate_ranker)
  4. Run SignalEngine on top-N candidates
  5. For each valid signal:
       a. Run ML filter
       b. Run AI confidence scorer
       c. If approved → display table, notify Pushover, save to DB
       d. Optionally route to execution layer
  6. Update trailing stops on open positions
  7. Check exit conditions on open positions
  8. Sleep until next candle opens

The scanner can be run in two modes:
  • Dry run (default) — signals printed, never executed
  • Live mode         — orders sent via execution layer
"""

from __future__ import annotations

import logging
import signal as _signal
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import pytz

log = logging.getLogger(__name__)

try:
    from src.config import (
        TIMEZONE, MAX_RISK_PER_TRADE, STOP_LOSS_PCT,
        MAX_DAILY_DRAWDOWN, MAX_TRADES_PER_HOUR, ACCOUNT_BALANCE,
        ML_CONFIDENCE_THRESHOLD, AI_CONFIDENCE_THRESHOLD,
    )
    from src.data.heikin_ashi      import convert_to_heikin_ashi
    from src.data.market_data      import get_latest_candles
    from src.data.symbol_mapper    import get_all_symbols, get_asset_class, best_source
    from src.data.db               import init_db, save_signal, save_trade_open, save_trade_close
    from src.signals.signal_engine import SignalEngine
    from src.risk.risk_manager     import RiskManager
    from src.risk.position_sizer   import calculate_position_size
    from src.risk.trailing_stop    import TrailingStop
    from src.signals.types         import TradeRecord, BuySignalResult, SellSignalResult
    from src.scanner.candidate_ranker import score_symbol, rank_candidates
    from src.display.tables        import (
        print_buy_signal, print_sell_signal, print_trade_closed, print_kill_switch,
        print_active_signals, print_trail_update,
    )
    from src.notifications.logger  import (
        log_signal, log_trade_open, log_trade_close, log_rejection, log_kill_switch,
    )
    from src.notifications.pushover import (
        notify_buy_signal, notify_sell_signal, notify_trade_closed, notify_kill_switch,
    )
    from src.ml.model              import passes_ml_filter
    from src.ai.signal_ranker      import rank_signal
    from src.indicators.alligator  import calculate_alligator, check_lips_touch_teeth_down, check_lips_touch_teeth_up
except ImportError as e:
    log.error("Scanner import error: %s", e)
    raise

_tz = pytz.timezone(TIMEZONE)
_stop_event = threading.Event()


# ── Timeframe → sleep seconds ─────────────────────────────────────────────────
_TF_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}


class MarketScanner:
    """Real-time market monitoring and signal detection engine."""

    def __init__(
        self,
        symbols:           Optional[list[str]] = None,
        timeframe:         str   = "1h",
        top_candidates:    int   = 10,
        candles_lookback:  int   = 200,
        dry_run:           bool  = True,
        account_balance:   float = ACCOUNT_BALANCE,
    ) -> None:
        self.symbols          = symbols or get_all_symbols()
        self.timeframe        = timeframe
        self.top_candidates   = top_candidates
        self.candles_lookback = candles_lookback
        self.dry_run          = dry_run

        self.risk_manager = RiskManager(
            account_balance        = account_balance,
            max_risk_per_trade     = MAX_RISK_PER_TRADE,
            stop_loss_pct          = STOP_LOSS_PCT,
            max_daily_drawdown     = MAX_DAILY_DRAWDOWN,
            max_trades_per_hour    = MAX_TRADES_PER_HOUR,
        )
        # Active positions: trade_id → (TradeRecord, TrailingStop)
        self._open: dict[str, tuple[TradeRecord, TrailingStop]] = {}
        # Per-symbol SignalEngine instances
        self._engines: dict[str, SignalEngine] = {}

    def _get_engine(self, symbol: str) -> SignalEngine:
        if symbol not in self._engines:
            self._engines[symbol] = SignalEngine(symbol, self.timeframe)
        return self._engines[symbol]

    # ── Single scan cycle ─────────────────────────────────────────────────────

    def _scan_once(self) -> None:
        now_str = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        log.info("Scan cycle starting at %s for %d symbols", now_str, len(self.symbols))

        # Step 1: Fetch + convert HA for all symbols
        ha_data: dict[str, object] = {}  # symbol → ha_df
        for sym in self.symbols:
            try:
                src    = best_source(sym)
                raw_df = get_latest_candles(sym, self.timeframe, self.candles_lookback, source=src)
                if raw_df.empty:
                    continue
                ha_df = convert_to_heikin_ashi(raw_df)
                ha_data[sym] = ha_df
            except Exception as e:
                log.debug("Data fetch failed for %s: %s", sym, e)

        if not ha_data:
            log.warning("No data received for any symbol this cycle")
            return

        # Step 2: Score candidates
        scores = []
        for sym, ha_df in ha_data.items():
            sc = score_symbol(sym, self.timeframe, ha_df)
            if sc:
                scores.append(sc)
        top = rank_candidates(scores, top_n=self.top_candidates)
        top_symbols = {s.symbol for s in top}

        # Step 3: Update open positions first (trailing stop + exit checks)
        self._update_open_positions(ha_data)

        # Step 4: Signal detection on top candidates
        if self.risk_manager.is_kill_switch_active():
            log.warning("Kill switch active — skipping signal detection")
            return

        for sym in top_symbols:
            ha_df = ha_data.get(sym)
            if ha_df is None:
                continue
            if not self.risk_manager.can_open_trade():
                break   # hit hourly or daily limit

            try:
                self._evaluate_symbol(sym, ha_df)
            except Exception as e:
                log.error("Signal eval failed for %s: %s", sym, e)

    def _evaluate_symbol(self, sym: str, ha_df: object) -> None:
        engine = self._get_engine(sym)
        result = engine.evaluate_ha(ha_df)

        for sig_key in ("buy", "sell"):
            sig = result.get(sig_key)
            if sig is None or not sig.is_valid:
                continue

            ts_str = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

            # ML filter
            passed_ml, ml_prob = passes_ml_filter(sig)
            sig.ml_confidence = ml_prob
            if not passed_ml:
                log_rejection(sig.signal_type, sym, self.timeframe,
                              f"ML filter rejected (prob={ml_prob:.0%})")
                save_signal(sig)
                continue

            # AI confidence
            ai_score = rank_signal(sig)
            sig.ai_confidence = ai_score
            if ai_score < AI_CONFIDENCE_THRESHOLD:
                log_rejection(sig.signal_type, sym, self.timeframe,
                              f"AI rejected (score={ai_score:.0%})")
                save_signal(sig)
                continue

            # Risk manager approval
            approved, reason = self.risk_manager.approve_signal(sig)
            if not approved:
                log_rejection(sig.signal_type, sym, self.timeframe, reason)
                save_signal(sig)
                continue

            # Log + display
            log_signal(
                sig.signal_type, sym, self.timeframe, True,
                sig.points, sig.entry_price, sig.stop_loss,
                sig.ml_confidence,
            )
            save_signal(sig)

            if sig.signal_type == "BUY":
                print_buy_signal(sig)
                notify_buy_signal(sym, self.timeframe, sig.entry_price, sig.stop_loss,
                                  sig.profit_estimate_pct, sig.ml_confidence, ts_str)
            else:
                print_sell_signal(sig)
                notify_sell_signal(sym, self.timeframe, sig.entry_price, sig.stop_loss,
                                   sig.profit_estimate_pct, sig.ml_confidence, ts_str)

            if not self.dry_run:
                self._open_position(sig, ha_df)

    def _open_position(self, sig: object, ha_df: object) -> None:
        import uuid, numpy as np
        from src.signals.types import TradeRecord
        entry  = sig.entry_price
        sl     = sig.stop_loss
        size   = calculate_position_size(
            self.risk_manager.account_balance, entry, sl, MAX_RISK_PER_TRADE
        )
        if size <= 0:
            return

        last      = ha_df.iloc[-1]
        teeth_now = float(last.get("teeth", entry) or entry)

        trail = TrailingStop(
            direction    = "buy" if sig.signal_type == "BUY" else "sell",
            entry_price  = entry,
            initial_teeth= teeth_now,
            stop_loss_pct= STOP_LOSS_PCT,
        )
        rec = TradeRecord(
            trade_id        = str(uuid.uuid4()),
            signal_type     = sig.signal_type,
            asset           = sig.asset,
            timeframe       = sig.timeframe,
            entry_time      = datetime.now(_tz),
            entry_price     = entry,
            stop_loss_hard  = sl,
            trailing_stop   = trail.current_stop,
            position_size   = size,
            account_risk_pct= MAX_RISK_PER_TRADE,
            alligator_point = sig.alligator_point,
            stochastic_point= sig.stochastic_point,
            vortex_point    = sig.vortex_point,
            jaw_at_entry    = sig.jaw_price,
            teeth_at_entry  = sig.teeth_price,
            lips_at_entry   = sig.lips_price,
            ml_confidence   = sig.ml_confidence,
            ai_confidence   = sig.ai_confidence,
        )
        self._open[rec.trade_id] = (rec, trail)
        self.risk_manager.record_opened_from_record(rec)
        save_trade_open(rec)
        log_trade_open(rec.trade_id, sig.signal_type, sig.asset, sig.timeframe,
                       entry, sl, trail.current_stop, size, MAX_RISK_PER_TRADE)

    def _update_open_positions(self, ha_data: dict) -> None:
        import numpy as np
        from src.indicators.alligator import calculate_alligator
        to_close: list[str] = []

        for tid, (rec, trail) in list(self._open.items()):
            ha_df = ha_data.get(rec.asset)
            if ha_df is None:
                continue

            last = ha_df.iloc[-1]
            teeth_now = float(last.get("teeth", rec.entry_price) or rec.entry_price)
            if not np.isnan(teeth_now):
                old_stop = trail.current_stop
                new_stop = trail.update(teeth_now)
                if abs(new_stop - old_stop) > 1e-10:
                    rec.trailing_stop = new_stop
                    print_trail_update(rec.asset, old_stop, new_stop, rec.signal_type)

            close_price = float(last["ha_close"])
            close_reason: Optional[str] = None

            # Alligator TP check
            alligator_df = calculate_alligator(ha_df)
            if len(alligator_df) >= 2:
                if rec.signal_type == "BUY":
                    if check_lips_touch_teeth_down(alligator_df):
                        close_reason = "ALLIGATOR_TP"
                else:
                    if check_lips_touch_teeth_up(alligator_df):
                        close_reason = "ALLIGATOR_TP"

            if close_reason is None and trail.is_triggered(close_price):
                close_reason = "TRAIL_STOP"

            if close_reason is None:
                if rec.signal_type == "BUY"  and close_price <= rec.stop_loss_hard:
                    close_reason = "HARD_STOP"
                elif rec.signal_type == "SELL" and close_price >= rec.stop_loss_hard:
                    close_reason = "HARD_STOP"

            if close_reason:
                if rec.signal_type == "BUY":
                    pnl_pct = (close_price - rec.entry_price) / rec.entry_price * 100
                else:
                    pnl_pct = (rec.entry_price - close_price) / rec.entry_price * 100
                pnl = pnl_pct / 100 * rec.position_size * rec.entry_price

                rec.exit_time    = datetime.now(_tz)
                rec.exit_price   = close_price
                rec.close_reason = close_reason
                rec.pnl          = pnl
                rec.pnl_pct      = pnl_pct
                rec.status       = "CLOSED"

                self.risk_manager.record_closed_pnl(tid, pnl)
                save_trade_close(rec)
                print_trade_closed(rec)
                ts_str = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                notify_trade_closed(tid, rec.asset, rec.signal_type, pnl, pnl_pct, close_reason, ts_str)
                log_trade_close(tid, rec.signal_type, rec.asset,
                                rec.entry_time, rec.exit_time,
                                rec.entry_price, close_price, close_reason, pnl, pnl_pct,
                                rec.max_trail_reached)

                if self.risk_manager.is_kill_switch_active():
                    loss_pct = abs(self.risk_manager.daily_pnl / self.risk_manager.daily_start_balance * 100)
                    print_kill_switch(loss_pct)
                    log_kill_switch(loss_pct)
                    notify_kill_switch(loss_pct, ts_str)

                to_close.append(tid)

        for tid in to_close:
            self._open.pop(tid, None)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Block and run scan cycles until stop() is called or SIGINT received."""
        _stop_event.clear()
        init_db()
        sleep_s = _TF_SECONDS.get(self.timeframe, 3600)
        mode    = "DRY RUN" if self.dry_run else "LIVE TRADING"
        log.info("Scanner started — %s — timeframe: %s — %d symbols — interval: %ds",
                 mode, self.timeframe, len(self.symbols), sleep_s)

        def _handle_stop(sig, frame):
            log.info("Stop signal received, shutting down scanner...")
            _stop_event.set()

        _signal.signal(_signal.SIGINT,  _handle_stop)
        _signal.signal(_signal.SIGTERM, _handle_stop)

        while not _stop_event.is_set():
            try:
                self._scan_once()
                print_active_signals(list(rec for rec, _ in self._open.values()))
            except Exception as e:
                log.error("Scan cycle error: %s", e)
            # Sleep, but wake up if stop is signalled
            _stop_event.wait(timeout=sleep_s)

        log.info("Scanner stopped cleanly.")

    def stop(self) -> None:
        """Signal the scanner loop to exit."""
        _stop_event.set()
