"""IBKR Adapter — executes trades via Interactive Brokers (IBKR) API.

Requires IBKR_CLIENT_ID, IBKR_PORT, IBKR_HOST in .env

Implements the same interface as FPMarketsAdapter for compatibility with BrokerRouter.
"""

from ib_insync import IB, MarketOrder, util
from src.config import IBKR_CLIENT_ID, IBKR_PORT, IBKR_HOST

class IBKRAdapter:
    def __init__(self):
        self.client_id = int(IBKR_CLIENT_ID)
        self.port = int(IBKR_PORT)
        self.host = IBKR_HOST
        self.ib = IB()
        self.connected = False

    def connect(self):
        try:
            self.ib.connect(self.host, self.port, self.client_id)
            self.connected = self.ib.isConnected()
            return self.connected
        except Exception as e:
            print(f"[IBKRAdapter] Connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        self.ib.disconnect()
        self.connected = False

    def place_order(self, signal_type, symbol, volume, expected_entry, stop_loss, trade_id):
        if not self.connected:
            raise RuntimeError("IBKRAdapter not connected")
        action = 'BUY' if signal_type.upper() == 'BUY' else 'SELL'
        # For simplicity, assume stock order; for forex, use IBKR's Forex contract
        from ib_insync import Stock
        contract = Stock(symbol, 'SMART', 'USD')
        order = MarketOrder(action, volume)
        try:
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(1)
            fill = trade.fills[-1] if trade.fills else None
            return {
                'order_id': trade.order.permId,
                'symbol': symbol,
                'side': action,
                'volume': volume,
                'price': fill.price if fill else expected_entry,
                'status': trade.orderStatus.status,
                'raw': trade,
            }
        except Exception as e:
            print(f"[IBKRAdapter] Order failed: {e}")
            return None

    def close_order(self, trade_id):
        # Not implemented: would need to track open orders and send opposite order
        return True

    def update_trailing_stop(self, trade_id, new_sl):
        # Not implemented for this example
        return True

    def modify_position_sltp(self, trade_id, new_sl, new_tp=None):
        # Not implemented for this example
        return True
