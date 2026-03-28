"""Broker router — routes orders to the correct execution adapter.

Currently supported brokers:
  • FP Markets / MT5 — forex, commodities, indices, crypto CFDs

Asset-class routing:
  Forex      → FP Markets MT5
  Crypto     → FP Markets MT5  (CFD, NOT Binance)
  Stocks     → FP Markets MT5  (CFD)
  Commodities→ FP Markets MT5

Canada note: Binance / Alpaca are NOT used for execution (regulatory restriction).
CCXT / Finnhub are only used for reading market data.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


try:
    from src.execution.fp_markets_adapter import FPMarketsAdapter
    from src.execution.order_manager      import OrderManager
    from src.data.symbol_mapper           import get_asset_class
except ImportError as e:
    log.error("BrokerRouter import error: %s", e)
    raise

# Lazy import for Kraken and IBKR adapters
def _lazy_import_kraken():
    from src.execution.kraken_adapter import KrakenAdapter
    return KrakenAdapter()

def _lazy_import_ibkr():
    from src.execution.ibkr_adapter import IBKRAdapter
    return IBKRAdapter()


class BrokerRouter:
    """Routes trade requests to the appropriate broker adapter."""


    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run        = dry_run
        self._fp_adapter    = FPMarketsAdapter()
        self._fp_mgr        = OrderManager(self._fp_adapter)
        self._kraken_adapter = None
        self._ibkr_adapter   = None
        self._connected     = False


    def connect(self) -> bool:
        """Connect all broker adapters. Returns True if at least one succeeds."""
        if self.dry_run:
            log.info("BrokerRouter: dry-run mode — no broker connection")
            return True
        # Connect all adapters as needed
        fp_ok = self._fp_adapter.connect()
        # Lazy load Kraken and IBKR only if needed
        try:
            self._kraken_adapter = _lazy_import_kraken()
            kraken_ok = self._kraken_adapter.connect()
        except Exception:
            kraken_ok = False
        try:
            self._ibkr_adapter = _lazy_import_ibkr()
            ibkr_ok = self._ibkr_adapter.connect()
        except Exception:
            ibkr_ok = False
        self._connected = fp_ok or kraken_ok or ibkr_ok
        return self._connected


    def disconnect(self) -> None:
        self._fp_adapter.disconnect()
        if self._kraken_adapter:
            self._kraken_adapter.disconnect()
        if self._ibkr_adapter:
            self._ibkr_adapter.disconnect()
        self._connected = False

    def is_ready(self) -> bool:
        return self.dry_run or self._connected

    # ── Routing ────────────────────────────────────────────────────────────────


    def _get_manager(self, symbol: str):
        """Return the correct broker adapter or manager for a symbol."""
        asset_class = get_asset_class(symbol)
        if asset_class == "crypto":
            if not self._kraken_adapter:
                self._kraken_adapter = _lazy_import_kraken()
            return self._kraken_adapter
        elif asset_class == "stock":
            if not self._ibkr_adapter:
                self._ibkr_adapter = _lazy_import_ibkr()
            return self._ibkr_adapter
        else:
            return self._fp_mgr

    # ── Public API ─────────────────────────────────────────────────────────────

    def place_order(
        self,
        signal_type:    str,
        symbol:         str,
        volume:         float,
        expected_entry: float,
        stop_loss:      float,
        trade_id:       str,
    ) -> Optional[dict]:
        """Place a market order through the correct broker.

        Returns fill info dict or None (dry-run always returns None).
        """
        if self.dry_run:
            log.info("DRY RUN: would place %s %s vol=%.4f entry=%.5f sl=%.5f",
                     signal_type, symbol, volume, expected_entry, stop_loss)
            return None

        mgr = self._get_manager(symbol)
        if mgr is None:
            log.error("No broker adapter for %s", symbol)
            return None

        return mgr.place_order(
            signal_type    = signal_type,
            symbol         = symbol,
            volume         = volume,
            expected_entry = expected_entry,
            stop_loss      = stop_loss,
            trade_id       = trade_id,
        )

    def close_order(self, symbol: str, trade_id: str) -> bool:
        if self.dry_run:
            return True
        mgr = self._get_manager(symbol)
        return mgr.close_order(trade_id) if mgr else False

    def update_trailing_stop(self, symbol: str, trade_id: str, new_sl: float) -> bool:
        if self.dry_run:
            return True
        mgr = self._get_manager(symbol)
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
        mgr = self._get_manager(symbol)
        return mgr.modify_position_sltp(trade_id, new_sl, new_tp) if mgr else False

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
