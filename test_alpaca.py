   import os
from dotenv import load_dotenv
load_dotenv()
from alpaca.trading.client import TradingClient

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True
)

try:
    account = client.get_account()
    print(f"Alpaca account status: {account.status}")
    print(f"Account buying power: {account.buying_power}")
except Exception as e:
    print(f"Alpaca API connection failed: {e}")
