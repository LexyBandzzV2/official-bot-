"""Broker router — routes orders to the correct execution adapter.

This module implements *execution routing* (not data routing).

Design goals:
  - safe-by-default when API keys are missing (placeholders don't trade)
  - explicit live-mode routing rules (fees/capabilities)
  - permissive paper-mode routing (test everything)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


try:
    from src.execution.fp_markets_adapter import FPMarketsAdapter
    from src.execution.order_manager      import OrderManager
    from src.data.symbol_mapper           import get_asset_class
    from src.config import (
        TRADING_MODE,
        BROKER_PREFERENCE,
        ALPACA_ENABLED, KRAKEN_ENABLED, FXCM_ENABLED, IBKR_ENABLED, FP_MARKETS_ENABLED,
        ALPACA_MAX_TRADES_PER_HOUR, KRAKEN_MAX_TRADES_PER_HOUR,
        FXCM_MAX_TRADES_PER_HOUR, IBKR_MAX_TRADES_PER_HOUR,
        FP_MARKETS_LOGIN,
    )
    from src.data.db import save_broker_routing_decision, save_broker_execution
except ImportError as e:
    log.error("BrokerRouter import error: %s", e)
    raise

# Lazy import adapters (created only when used)
def _lazy_import_kraken():
    from src.execution.kraken_adapter import KrakenAdapter
    return KrakenAdapter()

def _lazy_import_ibkr():
    from src.execution.ibkr_adapter import IBKRAdapter
    return IBKRAdapter()

def _lazy_import_alpaca():
    from src.execution.alpaca_adapter import AlpacaAdapter
    return AlpacaAdapter()

def _lazy_import_fxcm():
    from src.execution.fxcm_adapter import FXCMAdapter
    return FXCMAdapter()


class BrokerRouter:
    """Routes trade requests to the appropriate broker adapter."""


    def __init__(self, dry_run: bool = True, preferred_broker: Optional[str] = None) -> None:
        self.dry_run        = dry_run
        pref = (preferred_broker or BROKER_PREFERENCE or "").strip().lower()
        self.preferred_broker = pref or None
        self._fp_adapter    = FPMarketsAdapter()
        self._fp_mgr        = OrderManager(self._fp_adapter)
        self._alpaca_adapter = None
        self._kraken_adapter = None
        self._fxcm_adapter   = None
        self._ibkr_adapter   = None
        self._connected     = False

        # per-broker hourly counters (router-level guard)
        self._hour_bucket = None
        self._broker_hour_counts = defaultdict(int)


    def connect(self) -> bool:
        """Connect broker adapters. When preferred_broker is set, connect only that broker."""
        if self.dry_run:
            log.info("BrokerRouter: dry-run mode — no broker connection")
            return True
        if self.preferred_broker:
            return self._connect_preferred_only()
        # Connect all adapters as needed (placeholders may fail gracefully)
        fp_ok = False
        if FP_MARKETS_ENABLED and FP_MARKETS_LOGIN:
            fp_ok = self._fp_adapter.connect()
        alpaca_ok = False
        kraken_ok = False
        fxcm_ok = False
        ibkr_ok = False

        try:
            self._alpaca_adapter = _lazy_import_alpaca()
            alpaca_ok = self._alpaca_adapter.connect()
        except Exception:
            alpaca_ok = False
        try:
            self._kraken_adapter = _lazy_import_kraken()
            kraken_ok = self._kraken_adapter.connect()
        except Exception:
            kraken_ok = False
        try:
            self._fxcm_adapter = _lazy_import_fxcm()
            fxcm_ok = self._fxcm_adapter.connect()
        except Exception:
            fxcm_ok = False
        try:
            self._ibkr_adapter = _lazy_import_ibkr()
            ibkr_ok = self._ibkr_adapter.connect()
        except Exception:
            ibkr_ok = False

        self._connected = fp_ok or alpaca_ok or kraken_ok or fxcm_ok or ibkr_ok
        return self._connected

    def _connect_preferred_only(self) -> bool:
        broker = self.preferred_broker
        if broker == "alpaca":
            if not ALPACA_ENABLED:
                log.warning("Preferred broker alpaca is disabled")
                return False
            try:
                self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
                self._connected = self._alpaca_adapter.connect()
                return self._connected
            except Exception:
                self._connected = False
                return False
        if broker == "kraken":
            if not KRAKEN_ENABLED:
                log.warning("Preferred broker kraken is disabled")
                return False
            try:
                self._kraken_adapter = self._kraken_adapter or _lazy_import_kraken()
                self._connected = self._kraken_adapter.connect()
                return self._connected
            except Exception:
                self._connected = False
                return False
        if broker == "fxcm":
            if not FXCM_ENABLED:
                log.warning("Preferred broker fxcm is disabled")
                return False
            try:
                self._fxcm_adapter = self._fxcm_adapter or _lazy_import_fxcm()
                self._connected = self._fxcm_adapter.connect()
                return self._connected
            except Exception:
                self._connected = False
                return False
        if broker == "ibkr":
            if not IBKR_ENABLED:
                log.warning("Preferred broker ibkr is disabled")
                return False
            try:
                self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
                self._connected = self._ibkr_adapter.connect()
                return self._connected
            except Exception:
                self._connected = False
                return False
        if broker == "fp":
            if not FP_MARKETS_ENABLED:
                log.warning("Preferred broker fp is disabled")
                return False
            self._connected = bool(FP_MARKETS_LOGIN and self._fp_adapter.connect())
            return self._connected
        log.warning("Unknown preferred broker: %s", broker)
        self._connected = False
        return False


    def disconnect(self) -> None:
        self._fp_adapter.disconnect()
        if self._alpaca_adapter:
            self._alpaca_adapter.disconnect()
        if self._kraken_adapter:
            self._kraken_adapter.disconnect()
        if self._fxcm_adapter:
            self._fxcm_adapter.disconnect()
        if self._ibkr_adapter:
            self._ibkr_adapter.disconnect()
        self._connected = False

    def is_ready(self) -> bool:
        return self.dry_run or self._connected

    def can_trade(self, symbol: str, timeframe: Optional[str] = None) -> bool:
        """Return True if the router would select a non-disabled broker in current mode.

        This is used by scanners to avoid scanning symbols/timeframes that cannot be executed in live mode.
        """
        try:
            broker_name, _ = self._get_manager(symbol, timeframe=timeframe)
            if broker_name in ("alpaca", "kraken", "fxcm", "ibkr"):
                return True
            return broker_name == "fp"
        except Exception:
            return False

    # ── Routing ────────────────────────────────────────────────────────────────


    def _refresh_hour_bucket(self) -> None:
        bucket = datetime.utcnow().strftime("%Y-%m-%dT%H")
        if bucket != self._hour_bucket:
            self._hour_bucket = bucket
            self._broker_hour_counts = defaultdict(int)

    def _broker_cap(self, broker: str) -> int:
        return {
            "alpaca": ALPACA_MAX_TRADES_PER_HOUR,
            "kraken": KRAKEN_MAX_TRADES_PER_HOUR,
            "fxcm": FXCM_MAX_TRADES_PER_HOUR,
            "ibkr": IBKR_MAX_TRADES_PER_HOUR,
            "fp": 10_000,  # internal manager (MT5 adapter); cap handled elsewhere
        }.get(broker, 0)

    def _cap_allows(self, broker: str) -> bool:
        self._refresh_hour_bucket()
        return self._broker_hour_counts[broker] < self._broker_cap(broker)

    def _mark_used(self, broker: str) -> None:
        self._refresh_hour_bucket()
        self._broker_hour_counts[broker] += 1

    def _get_manager(self, symbol: str, timeframe: Optional[str] = None):
        """Return the correct broker adapter or manager for a symbol/timeframe.

        Live-mode routing rules (high-level):
          - IBKR: no crypto
          - Forex: prefer FXCM (fees), fallback IBKR/FP
          - Stocks/ETFs: Alpaca for lower TFs, IBKR for larger TFs
          - Crypto: Kraken or Alpaca
          - Everything else: IBKR or FP Markets
        """
        asset_class = get_asset_class(symbol)
        tf = (timeframe or "").lower()

        mode = (TRADING_MODE or "paper").lower()

        if self.preferred_broker:
            return self._get_preferred_manager(symbol, asset_class, tf)

        # Paper: Alpaca for US stocks + crypto (paper API); IBKR for forex / commodities / indices; Kraken fallback for crypto.
        if mode == "paper":
            if asset_class == "stock" and ALPACA_ENABLED:
                self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
                if self._alpaca_adapter.connect():
                    return ("alpaca", self._alpaca_adapter)
            if asset_class == "crypto" and ALPACA_ENABLED:
                self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
                if self._alpaca_adapter.connect():
                    return ("alpaca", self._alpaca_adapter)
            if asset_class == "crypto" and KRAKEN_ENABLED:
                self._kraken_adapter = self._kraken_adapter or _lazy_import_kraken()
                if self._kraken_adapter.connect():
                    return ("kraken", self._kraken_adapter)
            if asset_class == "crypto" and IBKR_ENABLED:
                self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
                if self._ibkr_adapter.connect():
                    return ("ibkr", self._ibkr_adapter)
            if asset_class in ("forex", "commodity", "index") and IBKR_ENABLED:
                self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
                if self._ibkr_adapter.connect():
                    return ("ibkr", self._ibkr_adapter)
            if asset_class == "stock" and IBKR_ENABLED:
                self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
                if self._ibkr_adapter.connect():
                    return ("ibkr", self._ibkr_adapter)
            if FP_MARKETS_LOGIN and self._fp_adapter.connect():
                return ("fp", self._fp_mgr)
            return (None, None)

        # Live mode: strict routing
        if asset_class == "crypto":
            if KRAKEN_ENABLED:
                self._kraken_adapter = self._kraken_adapter or _lazy_import_kraken()
                if self._kraken_adapter.connect():
                    return ("kraken", self._kraken_adapter)
            if ALPACA_ENABLED:
                self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
                if self._alpaca_adapter.connect():
                    return ("alpaca", self._alpaca_adapter)
            if FP_MARKETS_LOGIN and self._fp_adapter.connect():
                return ("fp", self._fp_mgr)
            return (None, None)

        if asset_class == "forex":
            if FXCM_ENABLED:
                self._fxcm_adapter = self._fxcm_adapter or _lazy_import_fxcm()
                if self._fxcm_adapter.connect():
                    return ("fxcm", self._fxcm_adapter)
            if IBKR_ENABLED:
                self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
                if self._ibkr_adapter.connect():
                    return ("ibkr", self._ibkr_adapter)
            if FP_MARKETS_LOGIN and self._fp_adapter.connect():
                return ("fp", self._fp_mgr)
            return (None, None)

        # stocks / etfs -> always alpaca if enabled
        if asset_class == "stock" and ALPACA_ENABLED:
            self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
            if self._alpaca_adapter.connect():
                return ("alpaca", self._alpaca_adapter)
        if asset_class == "stock" and IBKR_ENABLED:
            self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
            if self._ibkr_adapter.connect():
                return ("ibkr", self._ibkr_adapter)
        if asset_class == "stock" and FP_MARKETS_LOGIN and self._fp_adapter.connect():
            return ("fp", self._fp_mgr)

        # commodities / indices / unknown -> IBKR preferred, else FP
        if IBKR_ENABLED:
            self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
            if self._ibkr_adapter.connect():
                return ("ibkr", self._ibkr_adapter)
        if FP_MARKETS_LOGIN and self._fp_adapter.connect():
            return ("fp", self._fp_mgr)
        return (None, None)

    def _get_preferred_manager(self, symbol: str, asset_class: str, timeframe: str):
        broker = self.preferred_broker
        if broker == "alpaca":
            if asset_class not in ("stock", "crypto"):
                return (None, None)
            if not ALPACA_ENABLED:
                return (None, None)
            self._alpaca_adapter = self._alpaca_adapter or _lazy_import_alpaca()
            if self._alpaca_adapter.connect():
                return ("alpaca", self._alpaca_adapter)
            return (None, None)
        if broker == "kraken":
            if asset_class != "crypto":
                return (None, None)
            if not KRAKEN_ENABLED:
                return (None, None)
            self._kraken_adapter = self._kraken_adapter or _lazy_import_kraken()
            if self._kraken_adapter.connect():
                return ("kraken", self._kraken_adapter)
            return (None, None)
        if broker == "fxcm":
            if asset_class != "forex":
                return (None, None)
            if not FXCM_ENABLED:
                return (None, None)
            self._fxcm_adapter = self._fxcm_adapter or _lazy_import_fxcm()
            if self._fxcm_adapter.connect():
                return ("fxcm", self._fxcm_adapter)
            return (None, None)
        if broker == "ibkr":
            if asset_class == "crypto":
                return (None, None)
            if not IBKR_ENABLED:
                return (None, None)
            self._ibkr_adapter = self._ibkr_adapter or _lazy_import_ibkr()
            if self._ibkr_adapter.connect():
                return ("ibkr", self._ibkr_adapter)
            return (None, None)
        if broker == "fp":
            if FP_MARKETS_LOGIN and self._fp_adapter.connect():
                return ("fp", self._fp_mgr)
            return (None, None)
        return (None, None)

    # ── Public API ─────────────────────────────────────────────────────────────

    def can_short(self, symbol: str) -> bool:
        """Check if shorts are available for this symbol on the selected broker.

        Returns True if longs are needed (SELL signals never short directly in this bot),
        or if the broker can handle shorts. Fails open on unknown brokers.
        """
        try:
            # This bot uses longs only — SELL signals are handled by exiting existing long positions.
            # Short-selling is not part of the base strategy.
            # If you add short capability, add broker-specific checks here.
            return True
        except Exception as e:
            log.warning("Short availability check failed for %s: %s — allowing anyway", symbol, e)
            return True

    def place_order(
        self,
        signal_type:    str,
        symbol:         str,
        timeframe:      Optional[str],
        volume:         float,
        expected_entry: float,
        stop_loss:      float,
        trade_id:       str,
        take_profit:    Optional[float] = None,
    ) -> Optional[dict]:
        """Place a market order through the correct broker.

        Returns fill info dict or None (dry-run always returns None).
        """
        if self.dry_run:
            log.info("DRY RUN: would place %s %s vol=%.4f entry=%.5f sl=%.5f",
                     signal_type, symbol, volume, expected_entry, stop_loss)
            return None

        # Pre-flight check: ensure shorts are available if this is a short order
        # (Note: this bot only trades longs, so this is a safety placeholder)
        if signal_type == "SELL" and not self.can_short(symbol):
            log.error("Short not available for %s — rejecting SELL signal", symbol)
            return None

        broker_name, mgr = self._get_manager(symbol, timeframe=timeframe)
        if broker_name:
            try:
                save_broker_routing_decision(
                    trading_mode=TRADING_MODE,
                    asset=symbol,
                    timeframe=timeframe or "",
                    asset_class=get_asset_class(symbol),
                    broker_name=broker_name,
                    reason="router_rule",
                )
            except Exception:
                pass
        if mgr is None or broker_name is None:
            log.error(
                "No broker available to place order for %s %s — check broker credentials and ENABLED flags",
                signal_type, symbol,
            )
            return None
        if not self._cap_allows(broker_name):
            log.warning("Broker %s hourly cap reached — refusing order for %s", broker_name, symbol)
            return None

        req = {
            "signal_type": signal_type,
            "symbol": symbol,
            "timeframe": timeframe,
            "volume": volume,
            "expected_entry": expected_entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "trade_id": trade_id,
        }
        try:
            res = mgr.place_order(
                signal_type=signal_type,
                symbol=symbol,
                volume=volume,
                expected_entry=expected_entry,
                stop_loss=stop_loss,
                trade_id=trade_id,
                take_profit=take_profit,
            )
            if res is not None:
                self._mark_used(broker_name)
            try:
                save_broker_execution(
                    broker_name=broker_name,
                    action="place_order",
                    ok=res is not None,
                    trade_id=trade_id,
                    asset=symbol,
                    timeframe=timeframe or "",
                    request=req,
                    response=res or {},
                )
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                save_broker_execution(
                    broker_name=broker_name,
                    action="place_order",
                    ok=False,
                    trade_id=trade_id,
                    asset=symbol,
                    timeframe=timeframe or "",
                    request=req,
                    response={},
                    error_message=str(e),
                )
            except Exception:
                pass
            raise

    def close_order(self, symbol: str, trade_id: str) -> bool:
        if self.dry_run:
            return True
        broker_name, mgr = self._get_manager(symbol)
        if broker_name is None or mgr is None:
            return False
        ok = False
        err = ""
        try:
            ok = bool(mgr.close_order(trade_id)) if mgr else False
            return ok
        except Exception as e:
            err = str(e)
            raise
        finally:
            try:
                save_broker_execution(
                    broker_name=broker_name,
                    action="close_order",
                    ok=ok,
                    trade_id=trade_id,
                    asset=symbol,
                    request={"trade_id": trade_id},
                    response={},
                    error_message=err,
                )
            except Exception:
                pass

    def update_trailing_stop(self, symbol: str, trade_id: str, new_sl: float) -> bool:
        if self.dry_run:
            return True
        _, mgr = self._get_manager(symbol)
        return mgr.update_trailing_stop(trade_id, new_sl) if mgr else False

    def modify_position_sltp(
        self,
        symbol: str,
        trade_id: str,
        new_sl: float,
        new_tp: Optional[float] = None,
    ) -> bool:
        if self.dry_run:
            return True
        broker_name, mgr = self._get_manager(symbol)
        if broker_name is None or mgr is None:
            return False
        ok = False
        err = ""
        try:
            ok = bool(mgr.modify_position_sltp(trade_id, new_sl, new_tp)) if mgr else False
            return ok
        except Exception as e:
            err = str(e)
            raise
        finally:
            try:
                save_broker_execution(
                    broker_name=broker_name,
                    action="modify_sltp",
                    ok=ok,
                    trade_id=trade_id,
                    asset=symbol,
                    request={"trade_id": trade_id, "new_sl": new_sl, "new_tp": new_tp},
                    response={},
                    error_message=err,
                )
            except Exception:
                pass

    def get_account_balance(self) -> Optional[float]:
        if self.dry_run:
            return None
        return self._fp_adapter.get_account_balance()

    def get_open_positions(self) -> list[dict]:
        if self.dry_run:
            return []
        return self._fp_adapter.get_open_positions()

    def average_slippage(self) -> float:
        return self._fp_mgr.average_slippage()
