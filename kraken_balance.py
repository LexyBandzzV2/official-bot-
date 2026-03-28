# kraken_balance.py
import krakenex

# Initialize Kraken API
api = krakenex.API()

# Load API keys from 'kraken.key' file
api.load_key('kraken.key')

# Query account balance
balance = api.query_private('Balance')

# Print the balance
print(balance)
