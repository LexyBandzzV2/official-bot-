"""FP Markets / MetaTrader 5 broker adapter.

Wraps the MetaTrader5 Python SDK for order submission and position management.
MT5 is Windows-only. The SDK is: pip install MetaTrader5

Credentials come from .env:
  FP_MARKETS_LOGIN    — MT5 account number
  FP_MARKETS_PASSWORD — MT5 account password
  FP_MARKETS_SERVER   — FP Markets server name (e.g. "FPMarkets-Live")

This adapter is NOT used for market data — only for trade execution.
Market data always comes from Finnhub / CCXT / yfinance.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    _MT5_AVAILABLE = False
    log.warning("MetaTrader5 not installed — broker execution disabled")

try:
    from src.config import FP_MARKETS_LOGIN, FP_MARKETS_PASSWORD, FP_MARKETS_SERVER
except ImportError:
    FP_MARKETS_LOGIN    = ""
    FP_MARKETS_PASSWORD = ""
    FP_MARKETS_SERVER   = ""


class FPMarketsAdapter:
    """Thin wrapper around the MT5 Python SDK for FP Markets."""

    def __init__(self) -> None:
        self._connected = False

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Initialise and log in to MT5. Returns True on success."""
        if not _MT5_AVAILABLE:
            log.error("MetaTrader5 SDK not installed — cannot connect to FP Markets")
            return False
        if not FP_MARKETS_LOGIN:
            log.error("FP_MARKETS_LOGIN not set in .env")
            return False

        if not mt5.initialize():
            log.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        login_result = mt5.login(
            int(FP_MARKETS_LOGIN),
            password=FP_MARKETS_PASSWORD,
            server=FP_MARKETS_SERVER,
        )
        if not login_result:
            log.error("MT5 login failed: %s", mt5.last_error())
            mt5.shutdown()
            return False

        info = mt5.account_info()
        log.info("MT5 connected — server: %s | login: %s | balance: %.2f %s",
                 FP_MARKETS_SERVER, FP_MARKETS_LOGIN,
                 info.balance if info else 0, info.currency if info else "")
        self._connected = True
        return True

    def disconnect(self) -> None:
        if _MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 disconnected")

    def is_connected(self) -> bool:
        return self._connected and _MT5_AVAILABLE

    # ── Order submission ───────────────────────────────────────────────────────

    def market_order(
        self,
        symbol:      str,
        direction:   str,   # "BUY" or "SELL"
        volume:      float,
        sl_price:    float,
        tp_price:    Optional[float] = None,
        comment:     str = "AlgoBot",
    ) -> Optional[dict]:
        """Submit a market order with hard stop loss.

        Returns order result dict or None on failure.
        """
        if not self.is_connected():
            log.error("market_order: not connected to MT5")
            return None

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error("market_order: no tick for symbol %s", symbol)
            return None

        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    float(volume),
            "type":      order_type,
            "price":     price,
            "sl":        sl_price,
            "deviation": 20,
            "magic":     20240101,
            "comment":   comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if tp_price:
            request["tp"] = tp_price

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("market_order failed: %s", mt5.last_error() if result is None else result.comment)
            return None

        log.info("Market order placed: %s %s vol=%.4f price=%.5f sl=%.5f #%s",
                 direction, symbol, volume, price, sl_price, result.order)
        return {
            "order_id":  result.order,
            "volume":    volume,
            "price":     result.price,
            "direction": direction,
            "symbol":    symbol,
        }

    def close_position(self, position_ticket: int) -> bool:
        """Close an open position by ticket number."""
        if not self.is_connected():
            return False

        position = None
        for pos in mt5.positions_get():
            if pos.ticket == position_ticket:
                position = pos
                break

        if position is None:
            log.warning("close_position: ticket %d not found", position_ticket)
            return False

        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(position.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action":   mt5.TRADE_ACTION_DEAL,
            "symbol":   position.symbol,
            "volume":   position.volume,
            "type":     close_type,
            "position": position_ticket,
            "price":    price,
            "deviation":20,
            "magic":    20240101,
            "comment":  "AlgoBot close",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("close_position failed: ticket=%d err=%s", position_ticket, mt5.last_error())
            return False
        log.info("Position #%d closed", position_ticket)
        return True

    def update_stop_loss(self, position_ticket: int, new_sl: float) -> bool:
        """Modify the stop loss of an existing position (trailing stop ratchet)."""
        if not self.is_connected():
            return False
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": position_ticket,
            "sl":       new_sl,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("update_stop_loss failed: ticket=%d err=%s", position_ticket, mt5.last_error())
            return False
        return True

    def get_account_balance(self) -> Optional[float]:
        if not self.is_connected():
            return None
        info = mt5.account_info()
        return float(info.balance) if info else None

    def get_open_positions(self) -> list[dict]:
        if not self.is_connected():
            return []
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":   p.ticket,
                "symbol":   p.symbol,
                "type":     "BUY" if p.type == 0 else "SELL",
                "volume":   p.volume,
                "open_price": p.price_open,
                "sl":       p.sl,
                "tp":       p.tp,
                "profit":   p.profit,
            }
            for p in positions
        ]
