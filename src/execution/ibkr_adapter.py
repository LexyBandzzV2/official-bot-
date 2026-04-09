"""IBKR Adapter — executes trades via Interactive Brokers (IBKR) API.

Integrated with ib_insync for robust, asynchronous API handling.
Supports Stocks, Forex, Futures, and Crypto.

Connection Coordination:
- Uses client_id from config (default: execution uses client_id + 1)
- Market data client uses base client_id
- Execution adapter uses client_id + 1 to avoid conflicts
- Both can share the same TWS/Gateway instance
"""

import logging
import asyncio
from typing import Optional, Dict, Any, Callable, List
from ib_insync import IB, MarketOrder, LimitOrder, StopOrder, Order, Stock, Forex, Future, Crypto, Contract, Trade, util

from src.config import IBKR_CLIENT_ID, IBKR_PORT, IBKR_HOST
from src.data.symbol_mapper import get_asset_class, to_finnhub

log = logging.getLogger(__name__)

class IBKRAdapter:
    def __init__(self, client_id_offset: int = 1):
        """Initialize IBKR execution adapter.
        
        Args:
            client_id_offset: Offset to add to base client_id to avoid conflicts
                             with market data client (default: 1)
        """
        self.ib = IB()
        self.host = IBKR_HOST
        self.port = int(IBKR_PORT)
        # Use offset client ID to avoid conflicts with market data client
        self.client_id = int(IBKR_CLIENT_ID) + client_id_offset
        self._connected = False
        self._position_callbacks: list[Callable] = []
        self._active_trades: Dict[str, Dict[str, Any]] = {}  # trade_id -> {contract, main_trade, stop_order, tp_order}
        
        log.info(f"IBKRAdapter initialized with client_id={self.client_id}")


    async def connect_async(self) -> bool:
        """Asynchronous connection to TWS/Gateway."""
        try:
            if not self.ib.isConnected():
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                self._connected = True
                log.info(f"Connected to IBKR at {self.host}:{self.port}")
            return True
        except Exception as e:
            log.error(f"IBKR Connection failed: {e}")
            self._connected = False
            return False

    def connect(self) -> bool:
        """Synchronous wrapper for connectivity (for compatibility)."""
        try:
            if not self.ib.isConnected():
                self.ib.connect(self.host, self.port, clientId=self.client_id)
                self._connected = True
            return True
        except Exception as e:
            log.error(f"IBKR Sync Connection failed: {e}")
            return False

    def disconnect(self):
        self.ib.disconnect()
        self._connected = False
    
    def register_position_callback(self, callback: Callable[[str, str], None]):
        """Register a callback to be notified when positions open/close.
        
        Args:
            callback: Function(symbol, action) where action is 'open' or 'close'
        """
        self._position_callbacks.append(callback)
    
    def _notify_position_change(self, symbol: str, action: str):
        """Notify registered callbacks of position changes."""
        for callback in self._position_callbacks:
            try:
                callback(symbol, action)
            except Exception as e:
                log.error(f"Position callback error: {e}")


    def _build_contract(self, symbol: str) -> Optional[Contract]:
        """Resolves canonical symbol to appropriate IBKR Contract object."""
        asset_class = get_asset_class(symbol)
        
        # Simple heuristics for IBKR contract types
        if asset_class == "forex":
            # e.g., EURUSD -> Forex('EURUSD')
            return Forex(pair=symbol)
        
        elif asset_class == "crypto":
            # e.g., BTCUSD -> Crypto('BTC', 'PAXOS', 'USD')
            # Note: IBKR Crypto usually requires an exchange like 'PAXOS'
            base = symbol.replace("USD", "").replace("USDT", "")
            return Crypto(base, 'PAXOS', 'USD')
            
        elif asset_class == "stock":
            # e.g., AAPL -> Stock('AAPL', 'SMART', 'USD')
            return Stock(symbol, 'SMART', 'USD')
            
        elif asset_class == "commodity" or asset_class == "index":
            # Futures often need specific symbols/expirations. 
            # This is a simplified fallback; manual mapping might be needed for specific futures.
            # For XAUUSD (Gold), IBKR often uses specific pairs or CFD.
            if symbol == "XAUUSD":
                return Stock("XAUUSD", "SMART", "USD") # Simplified
            return Stock(symbol, 'SMART', 'USD')
        
        return None

    def place_order(
        self, 
        signal_type: str, 
        symbol: str, 
        volume: float, 
        expected_entry: float, 
        stop_loss: float, 
        trade_id: str,
        take_profit: Optional[float] = None
    ) -> Optional[dict]:
        """Places a market order with attached stop loss and take profit via IBKR.
        
        Args:
            signal_type: 'BUY' or 'SELL'
            symbol: Trading symbol
            volume: Position size
            expected_entry: Expected entry price
            stop_loss: Initial stop loss price (2% from entry)
            trade_id: Unique trade identifier
            take_profit: Optional take profit price
            
        Returns:
            Dictionary with order details or None on failure
        """
        if not self.ib.isConnected():
            if not self.connect():
                return None

        contract = self._build_contract(symbol)
        if not contract:
            log.error(f"Could not resolve IBKR contract for {symbol}")
            return None

        action = 'BUY' if signal_type.upper() == 'BUY' else 'SELL'
        
        try:
            # Place main market order
            main_order = MarketOrder(action, volume)
            main_trade = self.ib.placeOrder(contract, main_order)
            
            # Wait for fill
            self.ib.sleep(2)

            ost = main_trade.orderStatus
            avg = getattr(ost, "avgFillPrice", None) or 0.0
            try:
                avg = float(avg)
            except (TypeError, ValueError):
                avg = 0.0
            fill_price = avg if avg > 0 else float(expected_entry)
            
            # Place stop loss order (opposite side)
            stop_action = 'SELL' if action == 'BUY' else 'BUY'
            stop_order = StopOrder(stop_action, volume, stop_loss)
            stop_trade = self.ib.placeOrder(contract, stop_order)
            
            log.info(f"Placed stop loss order for {symbol} at {stop_loss}")
            
            # Place take profit order if provided (opposite side, limit order)
            tp_trade = None
            if take_profit:
                tp_action = 'SELL' if action == 'BUY' else 'BUY'
                tp_order = LimitOrder(tp_action, volume, take_profit)
                tp_trade = self.ib.placeOrder(contract, tp_order)
                log.info(f"Placed take profit order for {symbol} at {take_profit}")
            
            # Store trade information for later updates
            self._active_trades[trade_id] = {
                'contract': contract,
                'main_trade': main_trade,
                'stop_trade': stop_trade,
                'tp_trade': tp_trade,
                'symbol': symbol,
                'action': action,
                'volume': volume,
                'entry_price': fill_price
            }
            
            # Notify position callbacks that a position was opened
            self._notify_position_change(symbol, 'open')
            
            return {
                'order_id': str(main_trade.order.permId),
                'symbol': symbol,
                'side': action,
                'volume': volume,
                'price': fill_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'status': main_trade.orderStatus.status,
                'trade': main_trade,
                'stop_order_id': str(stop_trade.order.permId) if stop_trade else None,
                'tp_order_id': str(tp_trade.order.permId) if tp_trade else None,
            }
        except Exception as e:
            log.error(f"IBKR Order placement failed for {symbol}: {e}")
            return None

    def close_order(self, trade_id: str) -> bool:
        """Close a position by canceling stop/TP orders and placing reverse order.
        
        Args:
            trade_id: Trade identifier
            
        Returns:
            True if close was successful
        """
        if trade_id not in self._active_trades:
            log.warning(f"Trade {trade_id} not found in active trades")
            return False
        
        try:
            trade_info = self._active_trades[trade_id]
            contract = trade_info['contract']
            symbol = trade_info['symbol']
            volume = trade_info['volume']
            action = trade_info['action']
            
            # Cancel stop loss and take profit orders
            if trade_info.get('stop_trade'):
                self.ib.cancelOrder(trade_info['stop_trade'].order)
                log.info(f"Canceled stop loss order for {symbol}")
            
            if trade_info.get('tp_trade'):
                self.ib.cancelOrder(trade_info['tp_trade'].order)
                log.info(f"Canceled take profit order for {symbol}")
            
            # Place reverse market order to close position
            close_action = 'SELL' if action == 'BUY' else 'BUY'
            close_order = MarketOrder(close_action, volume)
            close_trade = self.ib.placeOrder(contract, close_order)
            
            self.ib.sleep(1)
            
            log.info(f"Closed position for {symbol}")
            
            # Remove from active trades
            del self._active_trades[trade_id]
            
            # Notify position callbacks
            self._notify_position_change(symbol, 'close')
            
            return True
            
        except Exception as e:
            log.error(f"Error closing order {trade_id}: {e}")
            return False

    def update_trailing_stop(self, trade_id: str, new_sl: float) -> bool:
        """Update trailing stop loss for an active position.
        
        Args:
            trade_id: Trade identifier
            new_sl: New stop loss price
            
        Returns:
            True if update was successful
        """
        if trade_id not in self._active_trades:
            log.warning(f"Trade {trade_id} not found in active trades")
            return False
        
        try:
            trade_info = self._active_trades[trade_id]
            contract = trade_info['contract']
            symbol = trade_info['symbol']
            volume = trade_info['volume']
            action = trade_info['action']
            
            # Cancel old stop order
            if trade_info.get('stop_trade'):
                self.ib.cancelOrder(trade_info['stop_trade'].order)
            
            # Place new stop order
            stop_action = 'SELL' if action == 'BUY' else 'BUY'
            new_stop_order = StopOrder(stop_action, volume, new_sl)
            new_stop_trade = self.ib.placeOrder(contract, new_stop_order)
            
            # Update stored trade info
            trade_info['stop_trade'] = new_stop_trade
            
            log.info(f"Updated stop loss for {symbol} to {new_sl}")
            
            return True
            
        except Exception as e:
            log.error(f"Error updating stop loss for {trade_id}: {e}")
            return False

    def modify_position_sltp(
        self, 
        trade_id: str, 
        new_sl: Optional[float] = None, 
        new_tp: Optional[float] = None
    ) -> bool:
        """Modify stop loss and/or take profit for an active position.
        
        Args:
            trade_id: Trade identifier
            new_sl: New stop loss price (optional)
            new_tp: New take profit price (optional)
            
        Returns:
            True if modification was successful
        """
        if trade_id not in self._active_trades:
            log.warning(f"Trade {trade_id} not found in active trades")
            return False
        
        try:
            trade_info = self._active_trades[trade_id]
            contract = trade_info['contract']
            symbol = trade_info['symbol']
            volume = trade_info['volume']
            action = trade_info['action']
            
            # Update stop loss if provided
            if new_sl is not None:
                if trade_info.get('stop_trade'):
                    self.ib.cancelOrder(trade_info['stop_trade'].order)
                
                stop_action = 'SELL' if action == 'BUY' else 'BUY'
                new_stop_order = StopOrder(stop_action, volume, new_sl)
                new_stop_trade = self.ib.placeOrder(contract, new_stop_order)
                trade_info['stop_trade'] = new_stop_trade
                
                log.info(f"Updated stop loss for {symbol} to {new_sl}")
            
            # Update take profit if provided
            if new_tp is not None:
                if trade_info.get('tp_trade'):
                    self.ib.cancelOrder(trade_info['tp_trade'].order)
                
                tp_action = 'SELL' if action == 'BUY' else 'BUY'
                new_tp_order = LimitOrder(tp_action, volume, new_tp)
                new_tp_trade = self.ib.placeOrder(contract, new_tp_order)
                trade_info['tp_trade'] = new_tp_trade
                
                log.info(f"Updated take profit for {symbol} to {new_tp}")
            
            return True
            
        except Exception as e:
            log.error(f"Error modifying SL/TP for {trade_id}: {e}")
            return False
