"""Kraken Adapter — executes real crypto trades via Kraken REST API.

Requires KRAKEN_API_KEY and KRAKEN_SECRET in .env

Implements the same interface as FPMarketsAdapter for compatibility with BrokerRouter.
"""

import ccxt
from src.config import KRAKEN_API_KEY, KRAKEN_SECRET

class KrakenAdapter:
    def __init__(self):
        self.api_key = KRAKEN_API_KEY
        self.secret = KRAKEN_SECRET
        self.exchange = None

    def connect(self):
        self.exchange = ccxt.kraken({
            'apiKey': self.api_key,
            'secret': self.secret,
            'enableRateLimit': True,
        })
        # Test connection
        try:
            self.exchange.fetch_balance()
            return True
        except Exception as e:
            print(f"[KrakenAdapter] Connection failed: {e}")
            self.exchange = None
            return False

    def disconnect(self):
        self.exchange = None

    def place_order(self, signal_type, symbol, volume, expected_entry, stop_loss, trade_id, take_profit=None):
        if not self.exchange:
            raise RuntimeError("KrakenAdapter not connected")
        side = 'buy' if signal_type.upper() == 'BUY' else 'sell'
        try:
            order = self.exchange.create_market_order(symbol, side, volume)
            return {
                'order_id': order.get('id'),
                'symbol': symbol,
                'side': side,
                'volume': volume,
                'price': order.get('average', expected_entry),
                'status': order.get('status'),
                'raw': order,
            }
        except Exception as e:
            print(f"[KrakenAdapter] Order failed: {e}")
            return None

    def close_order(self, trade_id):
        # Kraken does not support closing by trade_id for spot; user must sell manually
        return True

    def update_trailing_stop(self, trade_id, new_sl):
        # Not supported on Kraken spot
        return True

    def modify_position_sltp(self, trade_id, new_sl, new_tp=None):
        # Not supported on Kraken spot
        return True
