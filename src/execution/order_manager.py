"""Order manager — tracks fills, slippage, and reconciles with the bot's trade records.

After a market order is submitted through a broker adapter, OrderManager:
  • Records fill price and actual slippage vs expected entry
  • Links broker order ID to internal TradeRecord
  • Periodically reconciles open MT5 positions vs bot's open positions
  • Exposes a unified interface so the scanner never talks to the broker directly
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

try:
    from src.execution.fp_markets_adapter import FPMarketsAdapter
    from src.data.db import save_trade_open
    from src.config import TIMEZONE
    import pytz
    _tz = pytz.timezone(TIMEZONE)
except ImportError:
    FPMarketsAdapter = None  # type: ignore
    _tz = timezone.utc


class OrderManager:
    """Manages order lifecycle from submission to close."""

    def __init__(self, adapter: Optional[FPMarketsAdapter] = None) -> None:
        # Map: internal trade_id → broker order/ticket info
        self._ticket_map: dict[str, dict] = {}
        self._adapter = adapter

    def set_adapter(self, adapter: FPMarketsAdapter) -> None:
        self._adapter = adapter

    # ── Order submission ───────────────────────────────────────────────────────

    def place_order(
        self,
        signal_type:    str,
        symbol:         str,
        volume:         float,
        expected_entry: float,
        stop_loss:      float,
        trade_id:       str,
        take_profit:    float = None,
        comment:        str = "AlgoBot",
    ) -> Optional[dict]:
        """Submit a market order and record the fill.

        Returns fill info dict {order_id, fill_price, slippage_pips} or None.
        """
        if self._adapter is None:
            log.warning("OrderManager.place_order: no adapter set (dry-run mode)")
            return None

        # Pass take_profit if the adapter supports it
        if hasattr(self._adapter, 'market_order'):
            import inspect
            params = inspect.signature(self._adapter.market_order).parameters
            if 'tp_price' in params:
                result = self._adapter.market_order(
                    symbol    = symbol,
                    direction = signal_type,
                    volume    = volume,
                    sl_price  = stop_loss,
                    tp_price  = take_profit,
                    comment   = comment,
                )
            else:
                result = self._adapter.market_order(
                    symbol    = symbol,
                    direction = signal_type,
                    volume    = volume,
                    sl_price  = stop_loss,
                    comment   = comment,
                )
        else:
            result = None
        if result is None:
            return None

        fill_price    = result.get("price", expected_entry)
        slippage_pips = abs(fill_price - expected_entry) / _pip_size(symbol)

        self._ticket_map[trade_id] = {
            "order_id":     result["order_id"],
            "fill_price":   fill_price,
            "slippage_pips":slippage_pips,
            "symbol":       symbol,
            "direction":    signal_type,
            "volume":       volume,
        }
        log.info("Order filled: %s %s vol=%.4f fill=%.5f slip=%.1f pips #%s",
                 signal_type, symbol, volume, fill_price, slippage_pips, result["order_id"])
        return self._ticket_map[trade_id]

    def close_order(self, trade_id: str) -> bool:
        """Close the MT5 position linked to this trade_id."""
        info = self._ticket_map.get(trade_id)
        if info is None:
            log.warning("close_order: no ticket for trade_id %s", trade_id)
            return False
        if self._adapter is None:
            return False
        ok = self._adapter.close_position(info["order_id"])
        if ok:
            self._ticket_map.pop(trade_id, None)
        return ok

    def update_trailing_stop(self, trade_id: str, new_sl: float) -> bool:
        """Push an updated stop loss to the broker (trailing ratchet)."""
        return self.modify_position_sltp(trade_id, new_sl, new_tp=None)

    def modify_position_sltp(
        self,
        trade_id: str,
        new_sl: float,
        new_tp: Optional[float] = None,
    ) -> bool:
        """Modify SL/TP on the open position (preserves TP when ``new_tp`` is None)."""
        info = self._ticket_map.get(trade_id)
        if info is None or self._adapter is None:
            return False
        return self._adapter.modify_position_sltp(info["order_id"], new_sl, new_tp)

    # ── Reconciliation ─────────────────────────────────────────────────────────

    def reconcile(self, bot_open_ids: list[str]) -> list[str]:
        """Check broker positions against bot's open trade IDs.

        Returns list of trade_ids that are open in the bot but NOT in the broker
        (these may have been closed by SL/TP at the broker level).
        """
        if self._adapter is None:
            return []
        broker_tickets = {p["ticket"] for p in self._adapter.get_open_positions()}
        missing = []
        for tid in bot_open_ids:
            info = self._ticket_map.get(tid)
            if info and info["order_id"] not in broker_tickets:
                missing.append(tid)
        if missing:
            log.warning("Reconciliation: %d trade(s) closed at broker but not in bot: %s",
                        len(missing), missing)
        return missing

    # ── Slippage stats ─────────────────────────────────────────────────────────

    def average_slippage(self) -> float:
        """Return average slippage in pips across all filled orders this session."""
        slippages = [v["slippage_pips"] for v in self._ticket_map.values() if "slippage_pips" in v]
        return sum(slippages) / len(slippages) if slippages else 0.0


def _pip_size(symbol: str) -> float:
    """Best-guess pip size for slippage calculation."""
    sym_upper = symbol.upper()
    if "JPY" in sym_upper:
        return 0.01
    if any(c in sym_upper for c in ["BTC", "ETH", "XRP"]):
        return 1.0
    return 0.0001
