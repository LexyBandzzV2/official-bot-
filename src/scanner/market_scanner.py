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

from collections import Counter
import logging
import signal as _signal
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    from src.execution.broker_router import BrokerRouter

import pytz

log = logging.getLogger(__name__)

try:
    from src.config import (
        TIMEZONE, MAX_RISK_PER_TRADE, STOP_LOSS_PCT,
        MAX_DAILY_DRAWDOWN, MAX_TRADES_PER_HOUR, ACCOUNT_BALANCE,
        AI_CONFIDENCE_THRESHOLD,
        PEAK_GIVEBACK_ENABLED, PEAK_GIVEBACK_FRACTION, PEAK_GIVEBACK_MIN_MFE_PCT,
        TRADING_MODE,
        SCALP_ATR_MULTIPLIER, SCALP_MOMENTUM_FADE_WINDOW, SCALP_MOMENTUM_FADE_TIGHTEN_FRAC,
    )
    from src.data.heikin_ashi      import convert_to_heikin_ashi
    from src.data.market_data      import get_latest_candles
    from src.data.symbol_mapper    import get_all_symbols, get_asset_class, best_source
    from src.data.db               import init_db, save_signal, save_trade_open, save_trade_close, save_lifecycle_event, update_trade_lifecycle
    from src.risk.exit_policies    import get_exit_policy, policy_state_name, FORMAL_TIMEFRAMES
    from src.risk.candle_quality   import momentum_fade_detected, evaluate_fade
    from src.signals.strategy_mode import is_formal_timeframe
    from src.signals.signal_engine import SignalEngine
    from src.risk.risk_manager     import RiskManager
    from src.risk.position_sizer   import calculate_position_size
    from src.risk.trailing_stop    import TrailingStop
    from src.risk.trailing_take_profit import PeakGiveback
    from src.risk.alligator_trailing_tp import AlligatorTrailingTP
    from src.signals.types         import TradeRecord, BuySignalResult, SellSignalResult
    from src.scanner.candidate_ranker import score_symbol, rank_candidates
    # Final Sprint: asset universe + prefilter layer
    from src.scanner.asset_universe import filter_to_universe, get_entry as _universe_get_entry
    from src.scanner.prefilters import run_prefilter
    from src.signals.strategy_mode import timeframe_to_mode as _timeframe_to_mode
    from src.display.tables        import (
        print_buy_signal, print_sell_signal, print_trade_closed, print_kill_switch,
        print_active_signals, print_trail_update,
    )
    from src.notifications.logger  import (
        log_signal, log_trade_open, log_trade_close, log_rejection, log_kill_switch,
        log_trail_update_full, log_break_even_armed, log_profit_lock_stage,
    )
    from src.notifications.pushover import (
        notify_buy_signal, notify_sell_signal, notify_trade_closed, notify_kill_switch,
    )
    from src.ai.signal_ranker      import rank_signal
    from src.signals.score_engine  import compute_score, apply_ai_effect
    from src.indicators.alligator  import calculate_alligator, check_lips_touch_teeth_down, check_lips_touch_teeth_up
    from src.notifications.trade_candidate_logger import get_trade_candidate_logger
    # Phase 11: regime engine + gating (optional — scanner degrades gracefully without it)
    try:
        from src.signals.regime_engine import classify as _regime_classify, should_persist as _regime_should_persist
        from src.signals.regime_gating import (
            resolve_ai_threshold as _resolve_ai_threshold,
            resolve_position_size_factor as _resolve_size_factor,
            build_regime_context_for_signal as _build_regime_ctx,
        )
        from src.data.db import save_regime_snapshot as _save_regime_snapshot, get_latest_regime_snapshot as _get_latest_regime_snapshot
        from src.signals.regime_types import RegimeSnapshot as _RegimeSnapshot
        _REGIME_AVAILABLE = True
    except Exception as _regime_import_err:
        log.debug("Regime engine unavailable: %s — scanner will run without regime context", _regime_import_err)
        _REGIME_AVAILABLE = False

    # Phase 12: regime-aware adaptation (optional — scanner degrades gracefully without it)
    try:
        from src.signals.regime_adapter import (
            apply_regime_score_bias as _apply_regime_score_bias,
            check_regime_entry_filter as _check_regime_entry_filter,
            adapt_exit_params as _adapt_exit_params,
        )
        _REGIME_ADAPTER_AVAILABLE = True
    except Exception as _adapter_import_err:
        log.debug("Regime adapter unavailable: %s", _adapter_import_err)
        _REGIME_ADAPTER_AVAILABLE = False

    # Phase 14: live suitability resolver (optional — scanner degrades gracefully without it)
    try:
        from src.signals.suitability_resolver import SuitabilityResolver as _SuitabilityResolverClass
        _SUITABILITY_AVAILABLE = True
    except Exception as _suit_import_err:
        log.debug("Suitability resolver unavailable: %s", _suit_import_err)
        _SuitabilityResolverClass = None  # type: ignore[assignment,misc]
        _SUITABILITY_AVAILABLE = False
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
        data_source:       Optional[str] = None,  # New: force data source (ccxt, finnhub, yfinance, ibkr)
        execution_broker:  Optional[str] = None,
    ) -> None:
        self.symbols          = symbols or get_all_symbols()
        self.timeframe        = timeframe
        self.top_candidates   = top_candidates
        self.candles_lookback = candles_lookback
        self.dry_run          = dry_run
        self.data_source      = data_source  # Force specific data source if set
        self.execution_broker = (execution_broker or "").strip().lower() or None

        self.risk_manager = RiskManager(
            account_balance        = account_balance,
            max_risk_per_trade     = MAX_RISK_PER_TRADE,
            stop_loss_pct          = STOP_LOSS_PCT,
            max_daily_drawdown     = MAX_DAILY_DRAWDOWN,
            max_trades_per_hour    = MAX_TRADES_PER_HOUR,
        )
        # trade_id → (TradeRecord, TrailingStop, optional PeakGiveback, optional AlligatorTrailingTP)
        self._open: dict[str, tuple[TradeRecord, TrailingStop, Optional[PeakGiveback], Optional[AlligatorTrailingTP]]] = {}
        self._engines: dict[str, SignalEngine] = {}
        self._broker: Optional["BrokerRouter"] = None
        
        # Initialize trade candidate logger
        self._candidate_logger = get_trade_candidate_logger()
        # Phase 11: per-(symbol, timeframe) regime snapshot cache for change detection
        self._regime_cache: dict[str, Any] = {}   # key: "symbol|timeframe" → RegimeSnapshot
        # Phase 14: suitability resolver (one instance shared across all symbols per scan loop)
        self._suitability_resolver: Any = (
            _SuitabilityResolverClass() if _SUITABILITY_AVAILABLE and _SuitabilityResolverClass else None
        )
        
        if not dry_run:
            try:
                from src.execution.broker_router import BrokerRouter
                self._broker = BrokerRouter(dry_run=False, preferred_broker=self.execution_broker)
                if not self._broker.connect():
                    log.warning("Broker connect failed — live orders disabled for this session")
                    self._broker = None
            except Exception as e:
                log.warning("Broker init failed: %s — live orders disabled", e)
                self._broker = None

        # Execution routing restrictions: don't even scan symbols we can't execute.
        if (not self.dry_run) and self._broker and ((TRADING_MODE == "live") or self.execution_broker):
            before = len(self.symbols)
            self.symbols = [s for s in self.symbols if self._broker and self._broker.can_trade(s, timeframe=self.timeframe)]
            after = len(self.symbols)
            if after != before:
                broker_suffix = f" broker={self.execution_broker}" if self.execution_broker else ""
                log.info("Execution routing filter: %d -> %d symbols for timeframe=%s%s", before, after, self.timeframe, broker_suffix)

        # Final Sprint: universe filter — drop symbols whose group is disabled
        _before_universe = len(self.symbols)
        self.symbols = filter_to_universe(self.symbols)
        _after_universe = len(self.symbols)
        if _after_universe != _before_universe:
            log.info("Universe filter: %d -> %d symbols", _before_universe, _after_universe)

    def _get_engine(self, symbol: str) -> SignalEngine:
        if symbol not in self._engines:
            self._engines[symbol] = SignalEngine(symbol, self.timeframe)
        return self._engines[symbol]

    def _reject_signal(
        self,
        sig: BuySignalResult | SellSignalResult,
        ha_df: pd.DataFrame,
        reason: str,
        *,
        save_rejection: bool = True,
    ) -> None:
        reason_text = str(reason or "unspecified_rejection")
        sig.rejection_reason = reason_text
        log_rejection(
            sig.signal_type, sig.asset, self.timeframe, reason_text,
            entry=getattr(sig, "entry_price", None),
            ai_conf=getattr(sig, "ai_confidence", None),
        )
        if save_rejection:
            save_signal(sig)
        self._candidate_logger.log_from_signal(
            sig,
            ha_df,
            trade_sent_to_ibkr=False,
            rejection_reason=reason_text,
        )

    # ── Phase 11: per-cycle regime classification ─────────────────────────────

    def _classify_regime(self, sym: str, ha_df: object) -> Optional[Any]:
        """Classify regime for (sym, self.timeframe) and persist if changed.

        Returns a RegimeContext ready for signal injection, or None when the
        regime engine is unavailable or data is insufficient.
        """
        if not _REGIME_AVAILABLE:
            return None
        try:
            asset_class = ""
            try:
                asset_class = get_asset_class(sym) or ""
            except Exception:
                pass

            snapshot = _regime_classify(  # type: ignore[possibly-unbound]
                ha_df,
                asset       = sym,
                asset_class = asset_class,
                timeframe   = self.timeframe,
            )

            # Change-based persistence
            cache_key  = f"{sym}|{self.timeframe}"
            prev_snap  = self._regime_cache.get(cache_key)

            if _regime_should_persist(snapshot, prev_snap):  # type: ignore[possibly-unbound]
                try:
                    _save_regime_snapshot(snapshot)  # type: ignore[possibly-unbound]
                except Exception as exc:
                    log.debug("_classify_regime: save failed: %s", exc)
                self._regime_cache[cache_key] = snapshot
                log.info(
                    "Regime change: %s/%s → %s (conf=%.2f) | %s",
                    sym, self.timeframe,
                    snapshot.regime_label.value,
                    snapshot.confidence_score,
                    snapshot.evidence_summary,
                )
                try:
                    from src.display.tables import print_regime_change
                    print_regime_change(
                        sym, self.timeframe,
                        snapshot.regime_label.value,
                        float(snapshot.confidence_score),
                        str(snapshot.evidence_summary),
                    )
                except Exception:
                    pass
            else:
                self._regime_cache[cache_key] = snapshot

            return _build_regime_ctx(snapshot, previous_snapshot=prev_snap)  # type: ignore[possibly-unbound]
        except Exception as exc:
            log.debug("_classify_regime failed for %s: %s", sym, exc)
            return None

    # ── Single scan cycle ─────────────────────────────────────────────────────

    def _scan_once(self) -> None:
        now_str = datetime.now(_tz).strftime("%Y-%m-%d %I:%M:%S %p %Z")
        log.info("Scan cycle starting at %s for %d symbols", now_str, len(self.symbols))

        # Step 1: Fetch + convert HA for all symbols
        import pandas as _pd
        ha_data: dict[str, _pd.DataFrame] = {}  # symbol → ha_df
        for sym in self.symbols:
            try:
                # Use configured data source if set, otherwise auto-detect
                src = self.data_source if self.data_source else best_source(sym)
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
        # Step 2b: Final Sprint prefilter layer — volume gate only
        _mode_str = _timeframe_to_mode(self.timeframe).value
        prefilter_results: list = []
        for sc in scores:
            _ha = ha_data.get(sc.symbol)
            _avg_vol = 0.0
            if _ha is not None and "volume" in _ha.columns and len(_ha) >= 20:
                _avg_vol = float(_ha["volume"].tail(20).median())
            pfr = run_prefilter(
                symbol=sc.symbol,
                atr_pct=sc.atr_pct,
                volume_ratio=sc.volume_ratio,
                avg_volume=_avg_vol,
                mode=_mode_str,
                alligator_spread=sc.alligator_spread,
            )
            prefilter_results.append(pfr)

        prefilter_passed = [r for r in prefilter_results if r.passed]
        top_symbols = {r.symbol for r in prefilter_passed}

        if prefilter_results:
            skip_counts = Counter(r.skip_reason or "passed" for r in prefilter_results)
            top_survivors = ", ".join(
                f"{r.symbol}(vol={r.volume_ratio:.2f}x)"
                for r in prefilter_passed[:10]
            ) or "none"
            log.info(
                "Prefilter summary [%s]: scanned=%d passed=%d weak_vol=%d survivors=%s",
                self.timeframe,
                len(prefilter_results),
                len(prefilter_passed),
                skip_counts.get("blocked_by_weak_volume", 0),
                top_survivors,
            )

        # Build a lookup for prefilter metadata to stamp on signals later
        self._prefilter_lookup: dict = {r.symbol: r for r in prefilter_results}

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

    def _evaluate_symbol(self, sym: str, ha_df: pd.DataFrame) -> None:
        engine = self._get_engine(sym)
        result = engine.evaluate_ha(ha_df)

        # Phase 11: classify regime once per symbol/cycle before evaluating signals
        regime_ctx = self._classify_regime(sym, ha_df)

        for sig_key in ("buy", "sell"):
            sig = result.get(sig_key)
            if sig is None:
                continue
            if not sig.is_valid:
                if getattr(sig, "rejection_reason", "") == "CONFLICT_SUPPRESSED":
                    self._reject_signal(sig, ha_df, sig.rejection_reason)
                continue

            # Attach regime context to signal so it flows through the pipeline
            sig.regime_context = regime_ctx
            if regime_ctx is not None:
                log.debug("%s/%s signal %s: %s", sym, self.timeframe, sig_key.upper(),
                          regime_ctx.to_log_str())

            # Final Sprint: stamp prefilter audit fields on signal
            _pfr = getattr(self, "_prefilter_lookup", {}).get(sym)
            if _pfr is not None:
                sig.prefilter_universe_group = _pfr.universe_group
                sig.prefilter_atr_pct        = _pfr.atr_pct
                sig.prefilter_volume_ratio   = _pfr.volume_ratio
                sig.prefilter_rank_score     = _pfr.rank_score
                sig.prefilter_passed         = _pfr.passed
                sig.prefilter_skip_reason    = _pfr.skip_reason

            ts_str = datetime.now(_tz).strftime("%Y-%m-%d %I:%M:%S %p %Z")

            # Build enriched candle frame for score computation (candle quality + ATR volatility).
            try:
                from src.indicators.vortex import calculate_vortex
                from src.indicators.stochastic import calculate_stochastic
                from src.indicators.utils import calculate_atr
                import pandas as pd
                import numpy as np

                _score_df = calculate_alligator(ha_df)
                _score_df = calculate_vortex(_score_df)
                _score_df = calculate_stochastic(_score_df)
                atr = calculate_atr(_score_df, period=14)
                _score_df = _score_df.copy()
                _score_df["atr_14"] = atr
            except Exception:
                _score_df = None

            # Phase 5: compute score sub-components
            compute_score(sig, _score_df)

            # Phase 12: apply regime score bias (additive, after base score)
            if _REGIME_AVAILABLE and _REGIME_ADAPTER_AVAILABLE and regime_ctx is not None:
                _apply_regime_score_bias(sig, regime_ctx)  # type: ignore[possibly-unbound]

            # Phase 14: live suitability gating
            # Copy regime strings onto sig for DB persistence before resolver runs
            if regime_ctx is not None:
                _mr = getattr(regime_ctx, "macro_regime", None)
                _rl = getattr(regime_ctx, "regime_label", None)
                sig.macro_regime = getattr(_mr, "value", _mr)
                sig.regime_label = getattr(_rl, "value", _rl)

            _suit_decision = None
            if _SUITABILITY_AVAILABLE and self._suitability_resolver is not None:
                _suit_decision = self._suitability_resolver.resolve(sig, regime_ctx)
                sig.live_activation_decision = _suit_decision
                if _suit_decision.suitability_context is not None:
                    _sc = _suit_decision.suitability_context
                    sig.suitability_rating        = _sc.suitability_rating.value
                    sig.suitability_score         = _sc.suitability_score
                    sig.suitability_reason        = _sc.supporting_reason
                    sig.suitability_source_summary = _sc.source_summary
                    sig.suitability_context       = _sc
                sig.skip_reason_code           = _suit_decision.skip_reason_code
                sig.active_profile_snapshot_id = _suit_decision.active_profile_snapshot_id
                import json as _json
                try:
                    sig.decision_trace_json = _json.dumps(_suit_decision.to_trace_dict())
                except Exception:
                    pass

                # Apply score penalty before entry-filter
                if _suit_decision.score_penalty > 0:
                    sig.score_total = (float(getattr(sig, "score_total", 0.0))
                                       - _suit_decision.score_penalty)

                if not _suit_decision.allowed:
                    self._reject_signal(
                        sig,
                        ha_df,
                        _suit_decision.rejection_reason,
                    )
                    continue

            # AI confidence
            ai_score = rank_signal(sig)
            sig.ai_confidence = ai_score
            # Phase 5: apply AI effect to score
            # Phase 11: regime-adjusted AI threshold
            _ai_threshold = (
                _resolve_ai_threshold(AI_CONFIDENCE_THRESHOLD, regime_ctx)  # type: ignore[possibly-unbound]
                if _REGIME_AVAILABLE else AI_CONFIDENCE_THRESHOLD
            )
            apply_ai_effect(sig, ai_score, _ai_threshold)
            if ai_score < _ai_threshold:
                self._reject_signal(sig, ha_df, f"AI rejected (score={ai_score:.0%})")
                continue

            # Risk manager approval
            approved, reason = self.risk_manager.approve_signal(sig)
            if not approved:
                self._reject_signal(sig, ha_df, reason)
                continue

            # Phase 12: regime-aware entry filter (after all base gates, before final accept)
            # Phase 14: pass suitability threshold delta so friction layers compose cleanly
            if _REGIME_AVAILABLE and _REGIME_ADAPTER_AVAILABLE and regime_ctx is not None:
                _suit_delta = (
                    _suit_decision.threshold_delta
                    if (_suit_decision is not None and _suit_decision.threshold_delta > 0)
                    else 0.0
                )
                _entry_ok, _entry_reason = _check_regime_entry_filter(  # type: ignore[possibly-unbound]
                    sig, regime_ctx, extra_threshold_delta=_suit_delta
                )
                if not _entry_ok:
                    self._reject_signal(sig, ha_df, _entry_reason)
                    continue

            # All gates passed
            sig.accepted_signal = True
            # Append AI suffix to entry_reason_code
            _rc = sig.entry_reason_code or ""
            _ai = sig.ai_confidence
            if _ai is not None: _rc += f":ai{int(_ai * 100)}"
            sig.entry_reason_code = _rc or None

            # Log + display
            log_signal(
                sig.signal_type, sym, self.timeframe, True,
                sig.points, sig.entry_price, sig.stop_loss,
                None,
            )

            if sig.signal_type == "BUY":
                print_buy_signal(sig)
                notify_buy_signal(sym, self.timeframe, sig.entry_price, sig.stop_loss,
                                  sig.profit_estimate_pct, None, ts_str)
            else:
                print_sell_signal(sig)
                notify_sell_signal(sym, self.timeframe, sig.entry_price, sig.stop_loss,
                                   sig.profit_estimate_pct, None, ts_str)

            # Log approved candidate (before execution attempt)
            trade_sent = False
            execution_rejection = None
            if not self.dry_run:
                execution_rejection = self._open_position(sig, ha_df)
                trade_sent = execution_rejection is None

            if execution_rejection is not None:
                sig.accepted_signal = False
                sig.skip_reason_code = sig.skip_reason_code or "execution_blocked"
                self._reject_signal(sig, ha_df, execution_rejection)
                continue

            save_signal(sig)
            
            # Log the candidate with execution status
            self._candidate_logger.log_from_signal(
                sig, ha_df, trade_sent_to_ibkr=trade_sent, rejection_reason=None
            )

    def _open_position(self, sig: BuySignalResult | SellSignalResult, ha_df: pd.DataFrame) -> Optional[str]:
        import uuid
        import numpy as np
        from src.signals.types import TradeRecord
        entry  = sig.entry_price
        sl     = sig.stop_loss
        size   = calculate_position_size(
            self.risk_manager.account_balance, entry, sl, MAX_RISK_PER_TRADE
        )
        if size <= 0:
            log.debug(
                "_open_position: calculated size is 0 for %s — skipping",
                getattr(sig, "asset", "?"),
            )
            return "position_size_zero"

        # Phase 11: apply regime size factor (soft modifier; defaults to 1.0 = no change)
        _regime_ctx = getattr(sig, "regime_context", None)
        if _REGIME_AVAILABLE and _regime_ctx is not None:
            _size_factor = _resolve_size_factor(_regime_ctx)  # type: ignore[possibly-unbound]
            if _size_factor != 1.0:
                size = round(size * _size_factor, 6)
                log.debug(
                    "_open_position: regime size factor %.2f applied → size=%.6f [%s]",
                    _size_factor, size,
                    _regime_ctx.to_log_str() if hasattr(_regime_ctx, "to_log_str") else "",
                )
        if size <= 0:
            log.warning(
                "_open_position: regime size factor reduced size to 0 — skipping %s",
                getattr(sig, "asset", "?"),
            )
            return "regime_size_factor_zeroed"

        ag_df = calculate_alligator(ha_df)
        last  = ag_df.iloc[-1]
        teeth_now = float(last["teeth"]) if not np.isnan(last["teeth"]) else float(entry)
        lips_now = float(last["lips"]) if not np.isnan(last["lips"]) else float(entry)

        # Select mode-specific exit policy (Phase 3)
        policy = get_exit_policy(sig.timeframe)

        # Initialize trailing stop (tracks red line - teeth)
        trail = TrailingStop(
            direction    = "buy" if sig.signal_type == "BUY" else "sell",
            entry_price  = entry,
            initial_teeth= teeth_now,
            stop_loss_pct= STOP_LOSS_PCT,
        )
        
        # Initialize Alligator trailing take profit (tracks green line - lips)
        alligator_tp = AlligatorTrailingTP(
            direction="buy" if sig.signal_type == "BUY" else "sell",
            entry_price=entry,
            initial_lips=lips_now,
            min_profit_pct=0.01,  # 1% minimum profit before TP activates
        )
        
        # Initialize peak-giveback tracker using policy's giveback_frac (Phase 3).
        # IntermediateExitPolicy.giveback_frac == 0.35 == PEAK_GIVEBACK_FRACTION (no regression).
        # Phase 12: adapt exit params based on regime
        _adapted_gb = policy.giveback_frac
        _adapted_be = policy.break_even_pct
        _adapted_fade = policy.fade_tighten_frac
        if _REGIME_AVAILABLE and _REGIME_ADAPTER_AVAILABLE and _regime_ctx is not None:
            _adapted_gb, _adapted_be, _adapted_fade, _exit_reason = _adapt_exit_params(  # type: ignore[possibly-unbound]
                _regime_ctx, policy.giveback_frac, policy.break_even_pct, policy.fade_tighten_frac,
            )
            if _exit_reason:
                log.info(
                    "_open_position: regime exit adapt %s gb=%.3f→%.3f be=%.3f→%.3f fade=%.3f→%.3f",
                    sig.asset, policy.giveback_frac, _adapted_gb,
                    policy.break_even_pct, _adapted_be,
                    policy.fade_tighten_frac, _adapted_fade,
                )
        tp_track: Optional[PeakGiveback] = None
        if PEAK_GIVEBACK_ENABLED:
            tp_track = PeakGiveback(
                direction="buy" if sig.signal_type == "BUY" else "sell",
                entry_price=entry,
                giveback_frac=_adapted_gb,
                min_mfe_pct=policy.min_mfe_pct,
            )
            tp_track.update_bar(float(last["high"]), float(last["low"]))

        # Compose entry_reason (Phase 3) — built from sig fields set by Phase 5 worker + score engine
        _ai   = sig.ai_confidence
        _conf = f" | ai={_ai*100:.0f}%" if _ai is not None else ""
        _flags_str = sig.indicator_flags or "unknown"
        entry_reason = _flags_str + _conf

        # Phase 4/5: indicator_flags and entry_reason_code synced from sig
        # (workers set them in Phase C; scanner appended ML/AI suffix above)
        indicator_flags_str = sig.indicator_flags or "unknown"
        entry_reason_code   = sig.entry_reason_code or "unknown"

        # Phase 4: fallback policy tagging
        _is_formal = is_formal_timeframe(sig.timeframe)
        if not _is_formal:
            log.warning(
                "[FALLBACK POLICY] %s %s uses fallback exit policy %s "
                "(timeframe not in formal set — used_fallback_policy=True)",
                sig.asset, sig.timeframe, policy.name,
            )

        # Phase 4: initial policy-state name
        _initial_state = policy_state_name(policy.name, "INITIAL_STOP")

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
            ml_confidence   = None,
            ai_confidence   = sig.ai_confidence,
            strategy_mode   = sig.strategy_mode,
            # Phase 3 lifecycle fields set at open
            entry_reason         = entry_reason,
            initial_stop_value   = trail.current_stop,
            initial_exit_policy  = policy.name,
            # Phase 4 fields
            indicator_flags      = indicator_flags_str,
            entry_reason_code    = entry_reason_code,
            trail_active_mode    = "initial_stop",
            used_fallback_policy = not _is_formal,
            exit_policy_name     = _initial_state,
            # Phase 11: regime observability at trade open
            regime_label_at_entry      = (
                _regime_ctx.regime_label.value
                if _regime_ctx is not None and _regime_ctx.regime_label is not None
                else None
            ),
            regime_confidence_at_entry = (
                float(_regime_ctx.confidence_score)
                if _regime_ctx is not None else 0.0
            ),
            regime_snapshot_id         = (
                _regime_ctx.snapshot_id
                if _regime_ctx is not None else None
            ),
            # Phase 12: regime score adjustment at entry
            regime_score_adjustment    = (
                float(_regime_ctx.regime_score_adjustment)
                if _regime_ctx is not None else 0.0
            ),
            # Track best trailing stop level ever reached
            max_trail_reached = trail.current_stop,
        )
        rec._trail_stop = trail  # type: ignore[attr-defined]
        rec._alligator_tp = alligator_tp  # type: ignore[attr-defined]
        rec._exit_policy = policy  # type: ignore[attr-defined]
        # Phase 12: store adapted exit params for _update_open_positions
        rec._adapted_break_even_pct = _adapted_be  # type: ignore[attr-defined]
        rec._adapted_fade_tighten_frac = _adapted_fade  # type: ignore[attr-defined]
        rec._regime_ctx_at_entry = _regime_ctx  # type: ignore[attr-defined]

        if not self._broker:
            log.warning("_open_position: broker unavailable for %s %s", sig.signal_type, sig.asset)
            return "broker_unavailable"

        try:
            placed = self._broker.place_order(
                signal_type=sig.signal_type,
                symbol=sig.asset,
                timeframe=sig.timeframe,
                volume=size,
                expected_entry=entry,
                stop_loss=trail.current_stop,
                take_profit=None,
                trade_id=rec.trade_id,
            )
        except Exception as e:
            log.error("Broker place_order failed: %s", e)
            return f"broker_place_order_failed: {e}"

        if placed is None:
            log.warning("Order was not placed by broker router for %s %s", sig.signal_type, sig.asset)
            return "broker_router_rejected_or_not_placed"

        self._open[rec.trade_id] = (rec, trail, tp_track, alligator_tp)
        self.risk_manager.record_opened_from_record(rec)
        save_trade_open(rec)
        # Phase 3: record initial stop as first lifecycle event
        save_lifecycle_event(
            rec.trade_id, "trail_update",
            trail_update_reason="initial_stop",
            old_value=None,
            new_value=trail.current_stop,
            current_price=entry,
        )
        log_trade_open(rec.trade_id, sig.signal_type, sig.asset, sig.timeframe,
                       entry, sl, trail.current_stop, size, MAX_RISK_PER_TRADE,
                       rec.strategy_mode)

        log.info(
            "Placed order: %s %s @ %.4f, SL=%.4f",
            sig.signal_type, sig.asset, entry, trail.current_stop,
        )
        return None

    def _update_open_positions(self, ha_data: dict) -> None:
        import numpy as np
        to_close: list[str] = []

        for tid, position_data in list(self._open.items()):
            rec, trail, tp_track, alligator_tp = position_data
            
            ha_df = ha_data.get(rec.asset)
            if ha_df is None:
                continue

            ag_df = calculate_alligator(ha_df)
            last  = ag_df.iloc[-1]
            close_price = float(last["ha_close"])
            teeth_now = float(last["teeth"])
            lips_now = float(last["lips"])
            
            if np.isnan(teeth_now):
                teeth_now = float(rec.entry_price)
            if np.isnan(lips_now):
                lips_now = float(rec.entry_price)

            # Update trailing stop (tracks teeth - red line)
            old_stop = trail.current_stop
            new_stop = trail.update(teeth_now)

            # Sync max_trail_reached with the trail object on every bar
            rec.max_trail_reached = trail.max_trail
            
            # Update Alligator trailing TP (tracks lips - green line) for internal analytics
            if alligator_tp:
                alligator_tp.update(lips_now)

            # Update broker orders if stop changed (TP is managed internally for safety)
            if abs(new_stop - old_stop) > 1e-10:
                rec.trailing_stop = new_stop
                rec.trail_update_reason = "candle_trail"
                
                print_trail_update(rec.asset, old_stop, new_stop, rec.signal_type)
                log_trail_update_full(tid, rec.asset, old_stop, new_stop, "candle_trail", close_price)
                save_lifecycle_event(
                    tid, "trail_update",
                    trail_update_reason="candle_trail",
                    old_value=old_stop, new_value=new_stop,
                    current_price=close_price,
                )
                
                if self._broker:
                    try:
                        self._broker.modify_position_sltp(
                            rec.asset, tid, new_stop, None,
                        )
                    except Exception as e:
                        log.debug("Broker SL/TP update: %s", e)

            if tp_track is not None:
                tp_track.update_bar(float(last["high"]), float(last["low"]))

            # ── Phase 3: per-bar lifecycle tracking ──────────────────────────
            _now = datetime.now(_tz)

            # Compute unrealized PnL %
            if rec.signal_type == "BUY":
                unrealized_pct = (close_price - rec.entry_price) / rec.entry_price * 100.0
            else:
                unrealized_pct = (rec.entry_price - close_price) / rec.entry_price * 100.0

            # MFE tracking
            if unrealized_pct > rec.max_unrealized_profit:
                rec.max_unrealized_profit = unrealized_pct
                rec.timestamp_of_mfe = _now
                save_lifecycle_event(
                    tid, "mfe_update",
                    new_value=unrealized_pct, current_price=close_price,
                )
                update_trade_lifecycle(
                    tid,
                    max_unrealized_profit=unrealized_pct,
                    timestamp_of_mfe=_now.isoformat(),
                )

            # MAE tracking
            if unrealized_pct < rec.min_unrealized_profit:
                rec.min_unrealized_profit = unrealized_pct
                rec.timestamp_of_mae = _now
                save_lifecycle_event(
                    tid, "mae_update",
                    new_value=unrealized_pct, current_price=close_price,
                )
                update_trade_lifecycle(
                    tid,
                    min_unrealized_profit=unrealized_pct,
                    timestamp_of_mae=_now.isoformat(),
                )

            # Phase 12: track regime transitions during trade
            if _REGIME_AVAILABLE:
                try:
                    _current_ctx = self._classify_regime(rec.asset, ha_df)
                    if _current_ctx is not None and _current_ctx.regime_label is not None:
                        _current_label = _current_ctx.regime_label.value
                        if (
                            rec.regime_label_at_entry is not None
                            and _current_label != rec.regime_label_at_entry
                        ):
                            if not rec.regime_changed_during_trade:
                                rec.regime_changed_during_trade = True
                                rec.regime_transition_count = 1
                                log.info(
                                    "Regime transition during trade %s: %s → %s",
                                    tid, rec.regime_label_at_entry, _current_label,
                                )
                except Exception:
                    pass  # never block the update loop

            # Break-even arm check
            _policy = getattr(rec, "_exit_policy", None) or get_exit_policy(rec.timeframe)
            # Phase 12: use adapted break-even threshold if available
            _effective_be_pct = getattr(rec, "_adapted_break_even_pct", _policy.break_even_pct)
            if not rec.break_even_armed and unrealized_pct >= _effective_be_pct:
                _old_trail = trail.current_stop
                # Ratchet stop to at least entry price (never below current stop)
                if rec.signal_type == "BUY":
                    _be_level = max(trail.current_stop, rec.entry_price)
                else:
                    _be_level = min(trail.current_stop, rec.entry_price)
                trail.current_stop = _be_level
                rec.trailing_stop  = _be_level
                rec.break_even_armed = True
                rec.was_protected_profit = True
                rec.protected_profit_activation_time = _now
                rec.trail_update_reason = "break_even"
                # Phase 4: update richer state names
                rec.trail_active_mode = "break_even"
                rec.exit_policy_name  = policy_state_name(_policy.name, "BREAK_EVEN")
                log_trail_update_full(tid, rec.asset, _old_trail, _be_level, "break_even", close_price)
                log_break_even_armed(tid, rec.asset, rec.entry_price, close_price, unrealized_pct)
                save_lifecycle_event(
                    tid, "break_even_armed",
                    trail_update_reason="break_even",
                    old_value=_old_trail, new_value=_be_level,
                    current_price=close_price,
                )
                update_trade_lifecycle(
                    tid,
                    break_even_armed=1,
                    was_protected_profit=1,
                    protected_profit_activation_time=_now.isoformat(),
                    trailing_stop=_be_level,
                    trail_active_mode="break_even",
                    exit_policy_name=rec.exit_policy_name,
                )

            # Profit lock stage progression
            _next_stage = rec.profit_lock_stage + 1
            if _next_stage <= 3:
                _stages = _policy.profit_lock_stages
                _threshold_pct, _lock_pct = _stages[_next_stage - 1]
                if unrealized_pct >= _threshold_pct:
                    _old_trail = trail.current_stop
                    if rec.signal_type == "BUY":
                        _lock_level = max(trail.current_stop,
                                         rec.entry_price * (1.0 + _lock_pct / 100.0))
                    else:
                        _lock_level = min(trail.current_stop,
                                         rec.entry_price * (1.0 - _lock_pct / 100.0))
                    trail.current_stop = _lock_level
                    rec.trailing_stop  = _lock_level
                    rec.profit_lock_stage = _next_stage
                    rec.was_protected_profit = True
                    _reason = f"profit_lock_stage_{_next_stage}"
                    rec.trail_update_reason = _reason
                    # Phase 4: update richer state names
                    _stage_state = f"STAGE_{_next_stage}_LOCKED"
                    rec.trail_active_mode = f"stage_{_next_stage}"
                    rec.exit_policy_name  = policy_state_name(_policy.name, _stage_state)
                    log_trail_update_full(tid, rec.asset, _old_trail, _lock_level, _reason, close_price)
                    log_profit_lock_stage(tid, rec.asset, _next_stage, _lock_pct, close_price, unrealized_pct)
                    save_lifecycle_event(
                        tid, "profit_lock_stage",
                        trail_update_reason=_reason,
                        old_value=_old_trail, new_value=_lock_level,
                        current_price=close_price,
                        profit_lock_stage=_next_stage,
                    )
                    update_trade_lifecycle(
                        tid,
                        profit_lock_stage=_next_stage,
                        was_protected_profit=1,
                        trailing_stop=_lock_level,
                        trail_active_mode=rec.trail_active_mode,
                        exit_policy_name=rec.exit_policy_name,
                    )

            # ── Stage 4a: Candle-structure trailing (INTERMEDIATE / SWING) ───
            # Once all 3 profit-lock stages are reached and trail_mode is "candle",
            # trail under recent swing lows (BUY) or above recent highs (SELL).
            # Only ratchets tighter via trail.update(); never loosens.
            if (
                _policy.trail_mode == "candle"
                and rec.profit_lock_stage >= 3
            ):
                try:
                    from src.indicators.utils import get_recent_extremes
                    _lb = _policy.candle_trail_lookback_bars
                    _recent_low, _recent_high = get_recent_extremes(ha_df, lookback=_lb)
                    if rec.signal_type == "BUY":
                        _cs_candidate = _recent_low
                    else:
                        _cs_candidate = _recent_high
                    import math
                    if not math.isnan(_cs_candidate):
                        _old_stop_cs = trail.current_stop
                        _new_stop_cs = trail.update(float(_cs_candidate))
                        if abs(_new_stop_cs - _old_stop_cs) > 1e-10:
                            rec.trailing_stop       = _new_stop_cs
                            rec.trail_update_reason  = "candle_structure_trail"
                            rec.trail_active_mode    = "candle_structure_trail"
                            rec.exit_policy_name     = policy_state_name(_policy.name, "CANDLE_STRUCTURE_TRAIL")
                            log_trail_update_full(
                                tid, rec.asset, _old_stop_cs, _new_stop_cs,
                                "candle_structure_trail", close_price,
                            )
                            save_lifecycle_event(
                                tid, "trail_update",
                                trail_update_reason="candle_structure_trail",
                                old_value=_old_stop_cs, new_value=_new_stop_cs,
                                current_price=close_price,
                                profit_lock_stage=rec.profit_lock_stage,
                            )
                            update_trade_lifecycle(
                                tid,
                                trailing_stop=_new_stop_cs,
                                trail_active_mode="candle_structure_trail",
                                exit_policy_name=rec.exit_policy_name,
                            )
                except Exception as _cs_err:
                    log.debug("Candle-structure trail error for %s: %s", rec.asset, _cs_err)

            # ── Stage 4b: ATR trail (SCALP only, eligible after stage-2 lock) ─
            if (
                _policy.trail_mode == "atr"
                and rec.profit_lock_stage >= _policy.atr_eligible_after_stage
            ):
                try:
                    from src.indicators.utils import latest_atr
                    _atr_val = latest_atr(ha_df, period=14)
                    if _atr_val > 0:
                        _mult = _policy.atr_multiplier
                        if rec.signal_type == "BUY":
                            _atr_candidate = close_price - _atr_val * _mult
                        else:
                            _atr_candidate = close_price + _atr_val * _mult
                        _old_stop   = trail.current_stop
                        _new_stop   = trail.update(float(_atr_candidate))
                        if abs(_new_stop - _old_stop) > 1e-10:
                            rec.trailing_stop     = _new_stop
                            rec.trail_update_reason = "atr_trail"
                            rec.trail_active_mode = "atr_trail"
                            rec.exit_policy_name  = policy_state_name(_policy.name, "ATR_TRAIL")
                            log_trail_update_full(
                                tid, rec.asset, _old_stop, _new_stop, "atr_trail", close_price
                            )
                            save_lifecycle_event(
                                tid, "trail_update",
                                trail_update_reason="atr_trail",
                                old_value=_old_stop, new_value=_new_stop,
                                current_price=close_price,
                                profit_lock_stage=rec.profit_lock_stage,
                            )
                            update_trade_lifecycle(
                                tid,
                                trailing_stop=_new_stop,
                                trail_active_mode="atr_trail",
                                exit_policy_name=rec.exit_policy_name,
                            )
                except Exception as _atr_err:
                    log.debug("ATR trail error for %s: %s", rec.asset, _atr_err)

            # ── Phase 4/6: candle momentum-fade tightening ───────────────────
            # Applies to SCALP and INTERMEDIATE (SWING has momentum_fade_window=0).
            # Only tightens when profit is already locked (stage >= 1) and ATR
            # trail has NOT taken over yet.  Phase 6: uses structured evaluate_fade()
            # with per-policy thresholds; SCALP requires confirmation_bars=2 so a
            # single doji inside a healthy impulse cannot trigger tightening.
            if (
                _policy.momentum_fade_window > 0
                and rec.profit_lock_stage >= 1
                and rec.trail_active_mode != "atr_trail"
            ):
                try:
                    # Build last (window+1) HA candle tuples: (open, high, low, close)
                    _fw = _policy.momentum_fade_window
                    _needed = _fw + 1
                    if len(ha_df) >= _needed:
                        _tail = ha_df.iloc[-_needed:]
                        _candles = [
                            (
                                float(row["ha_open"]),
                                float(row["ha_high"]),
                                float(row["ha_low"]),
                                float(row["ha_close"]),
                            )
                            for _, row in _tail.iterrows()
                        ]
                        _fa = evaluate_fade(
                            _candles, rec.signal_type,
                            window=_fw,
                            weak_body_threshold=_policy.weak_body_threshold,
                            strong_body_threshold=_policy.strong_body_threshold,
                            adverse_wick_threshold=_policy.adverse_wick_threshold,
                            confirmation_bars=_policy.fade_confirmation_bars,
                        )
                        if _fa.fade_detected:
                            # Tighten using ATR-fraction candidate even in candle mode
                            try:
                                from src.indicators.utils import latest_atr
                                _atr_val2 = latest_atr(ha_df, period=14)
                            except Exception:
                                _atr_val2 = 0.0
                            if _atr_val2 > 0:
                                # Phase 12: use adapted fade tighten frac if available
                                _tighten = getattr(rec, "_adapted_fade_tighten_frac", _policy.fade_tighten_frac)
                                if rec.signal_type == "BUY":
                                    _fade_candidate = close_price - _atr_val2 * _tighten
                                else:
                                    _fade_candidate = close_price + _atr_val2 * _tighten
                                _old_stop2 = trail.current_stop
                                _new_stop2 = trail.update(float(_fade_candidate))
                                if abs(_new_stop2 - _old_stop2) > 1e-10:
                                    rec.trailing_stop     = _new_stop2
                                    rec.trail_update_reason = "momentum_fade"
                                    rec.trail_active_mode = "momentum_fade"
                                    rec.exit_policy_name  = policy_state_name(
                                        _policy.name, "MOMENTUM_FADE"
                                    )
                                    rec.fade_tighten_count = getattr(rec, "fade_tighten_count", 0) + 1
                                    rec.last_fade_body_ratio = _fa.last_body_ratio
                                    rec.last_fade_wick_ratio = _fa.last_wick_ratio_adverse
                                    log_trail_update_full(
                                        tid, rec.asset, _old_stop2, _new_stop2,
                                        "momentum_fade", close_price
                                    )
                                    save_lifecycle_event(
                                        tid, "trail_update",
                                        trail_update_reason="momentum_fade",
                                        old_value=_old_stop2, new_value=_new_stop2,
                                        current_price=close_price,
                                        profit_lock_stage=rec.profit_lock_stage,
                                        notes=_fa.evidence_summary(),
                                    )
                                    update_trade_lifecycle(
                                        tid,
                                        trailing_stop=_new_stop2,
                                        trail_active_mode="momentum_fade",
                                        exit_policy_name=rec.exit_policy_name,
                                        fade_tighten_count=rec.fade_tighten_count,
                                        last_fade_body_ratio=rec.last_fade_body_ratio,
                                        last_fade_wick_ratio=rec.last_fade_wick_ratio,
                                    )
                except Exception as _fade_err:
                    log.debug("Momentum-fade tightening error for %s: %s", rec.asset, _fade_err)

            should_close, close_reason = self.risk_manager.check_exit_conditions(
                tid,
                close_price,
                ha_df=ag_df,
                peak_giveback=tp_track,
            )

            if not should_close:
                continue

            if self._broker:
                try:
                    self._broker.close_order(rec.asset, tid)
                except Exception as e:
                    log.warning("Broker close_order: %s", e)

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
            # Phase 4: exit_policy_name already maintained per-bar;
            # only set it here if it was never updated past the initial state.
            _policy_at_close = getattr(rec, "_exit_policy", None) or get_exit_policy(rec.timeframe)
            if rec.exit_policy_name is None or rec.exit_policy_name == policy_state_name(
                _policy_at_close.name, "INITIAL_STOP"
            ):
                _stage = rec.profit_lock_stage
                if _stage >= 1:
                    rec.exit_policy_name = policy_state_name(
                        _policy_at_close.name, f"STAGE_{_stage}_LOCKED"
                    )
                else:
                    rec.exit_policy_name = policy_state_name(_policy_at_close.name, "INITIAL_STOP")

            self.risk_manager.record_closed_pnl(tid, pnl)

            # Phase 12: capture regime at exit and transition tracking
            if _REGIME_AVAILABLE:
                try:
                    _exit_regime_ctx = self._classify_regime(rec.asset, ha_df)
                    if _exit_regime_ctx is not None:
                        rec.regime_label_at_exit = (
                            _exit_regime_ctx.regime_label.value
                            if _exit_regime_ctx.regime_label is not None else None
                        )
                        rec.regime_confidence_at_exit = float(_exit_regime_ctx.confidence_score)
                        # Check if regime changed during the trade
                        if (
                            rec.regime_label_at_entry is not None
                            and rec.regime_label_at_exit is not None
                            and rec.regime_label_at_entry != rec.regime_label_at_exit
                        ):
                            rec.regime_changed_during_trade = True
                            rec.regime_transition_count = max(rec.regime_transition_count, 1)
                except Exception as _exit_regime_err:
                    log.debug("Phase 12: exit regime capture failed: %s", _exit_regime_err)

            save_trade_close(rec)
            print_trade_closed(rec)
            ts_str = datetime.now(_tz).strftime("%Y-%m-%d %I:%M:%S %p %Z")
            notify_trade_closed(tid, rec.asset, rec.signal_type, pnl, pnl_pct, close_reason, ts_str)
            log_trade_close(tid, rec.signal_type, rec.asset,
                            rec.entry_time, rec.exit_time,
                            rec.entry_price, close_price, close_reason, pnl, pnl_pct,
                            rec.max_trail_reached, rec.strategy_mode)

            if self.risk_manager.is_kill_switch_active():
                loss_pct = abs(self.risk_manager.daily_realised_pnl / self.risk_manager.daily_start_balance * 100)
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
                print_active_signals([position_data[0] for position_data in self._open.values()])
            except Exception as e:
                log.error("Scan cycle error: %s", e)
            # Sleep, but wake up if stop is signalled
            _stop_event.wait(timeout=sleep_s)

        log.info("Scanner stopped cleanly.")

    def stop(self) -> None:
        """Signal the scanner loop to exit."""
        _stop_event.set()
