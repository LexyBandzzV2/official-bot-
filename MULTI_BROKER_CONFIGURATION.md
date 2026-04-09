# Multi-Broker Configuration Guide

## Overview

This bot supports multiple brokers with intelligent routing based on asset class, timeframe, and trading mode (paper vs live).

## Broker Routing Rules

### Paper Trading Mode
**All brokers can scan ALL timeframes and ALL asset classes they support**
- No restrictions
- Used for testing and validation

### Live Trading Mode
**Strict routing based on fees and capabilities**

#### Alpaca
- **Assets**: Stocks, ETFs, Crypto
- **Timeframes**: 3m, 5m, 15m, 30m
- **Account Balance**: $50 (configurable)
- **Max Trades/Hour**: 15 (configurable)

#### Kraken
- **Assets**: Crypto, Forex (crypto exchange pairs)
- **Timeframes**: 3m, 5m, 15m, 30m
- **Account Balance**: $50 (configurable)
- **Max Trades/Hour**: 15 (configurable)

#### FXCM
- **Assets**: Forex (traditional currency pairs)
- **Timeframes**: All timeframes
- **Account Balance**: $50 (configurable)
- **Max Trades/Hour**: 15 (configurable)
- **Reason**: Lower fees than IBKR for forex

#### Interactive Brokers (IBKR)
- **Assets**: Everything else (Stocks, Commodities, Indices on larger timeframes)
- **Timeframes**: 15m, 30m, 1h, 2h, 4h (larger timeframes only)
- **Exclusions**: NO crypto, NO small timeframes (1m, 3m, 5m)
- **Account Balance**: $50 (configurable)
- **Max Trades/Hour**: 15 (configurable)

## Configuration Files

### 1. `.env` - Environment Variables

```bash
# ============================================================================
# MULTI-BROKER CONFIGURATION
# ============================================================================

# Trading Mode
TRADING_MODE=paper  # paper or live

# Global Risk Settings
MAX_TRADES_PER_HOUR=15  # Adjustable per-broker limit
STOP_LOSS_PCT=0.02      # 2%

# Peak-giveback exit — bar-close retracement guard.
# Canonical names (preferred):
PEAK_GIVEBACK_ENABLED=true      # enable bar-close retracement exit
PEAK_GIVEBACK_FRACTION=0.35     # 35% giveback of max favorable move triggers exit
# Legacy names still accepted as fallback (deprecated):
# TRAILING_TP_ENABLED=true
# TRAILING_TP_GIVEBACK=0.35

# ============================================================================
# ALPACA
# ============================================================================
ALPACA_API_KEY=your_alpaca_api_key_here
ALPACA_SECRET_KEY=your_alpaca_secret_key_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # paper trading
# ALPACA_BASE_URL=https://api.alpaca.markets      # live trading
ALPACA_ACCOUNT_BALANCE=50.00
ALPACA_MAX_TRADES_PER_HOUR=15
ALPACA_ENABLED=true

# Alpaca Asset Classes (live mode)
ALPACA_ASSETS=stocks,etfs,crypto
ALPACA_TIMEFRAMES=3m,5m,15m,30m

# ============================================================================
# KRAKEN
# ============================================================================
KRAKEN_API_KEY=your_kraken_api_key_here
KRAKEN_SECRET=your_kraken_secret_here
KRAKEN_ACCOUNT_BALANCE=50.00
KRAKEN_MAX_TRADES_PER_HOUR=15
KRAKEN_ENABLED=true

# Kraken Asset Classes (live mode)
KRAKEN_ASSETS=crypto,forex_crypto
KRAKEN_TIMEFRAMES=3m,5m,15m,30m

# ============================================================================
# FXCM
# ============================================================================
FXCM_API_KEY=your_fxcm_api_key_here
FXCM_ACCESS_TOKEN=your_fxcm_access_token_here
FXCM_ACCOUNT_TYPE=demo  # demo or real
FXCM_ACCOUNT_BALANCE=50.00
FXCM_MAX_TRADES_PER_HOUR=15
FXCM_ENABLED=true

# FXCM Asset Classes (live mode)
FXCM_ASSETS=forex
FXCM_TIMEFRAMES=all  # All timeframes supported

# ============================================================================
# INTERACTIVE BROKERS (IBKR)
# ============================================================================
IBKR_HOST=127.0.0.1
IBKR_PORT=7497  # 7497=paper, 7496=live
IBKR_CLIENT_ID=1
IBKR_ACCOUNT_BALANCE=50.00
IBKR_MAX_TRADES_PER_HOUR=15
IBKR_ENABLED=true

# IBKR Asset Classes (live mode)
IBKR_ASSETS=stocks,commodities,indices  # NO crypto
IBKR_TIMEFRAMES=15m,30m,1h,2h,4h  # Larger timeframes only

# ============================================================================
# LM STUDIO (Local Language Model)
# ============================================================================
LM_STUDIO_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=llama-3.1-8b-instruct
LM_STUDIO_ENABLED=true
LM_STUDIO_API_KEY=not-needed  # LM Studio doesn't require API key

# Alternative: Use OpenRouter if LM Studio not available
OPENROUTER_API_KEY=your_openrouter_key_here
OPENROUTER_MODEL=moonshotai/kimi-k2
OPENROUTER_ENABLED=false

# ============================================================================
# AI CONFIDENCE SCORING
# ============================================================================
AI_CONFIDENCE_THRESHOLD=0.60
ML_CONFIDENCE_THRESHOLD=0.60

# ============================================================================
# NOTIFICATIONS
# ============================================================================
PUSHOVER_APP_TOKEN=your_pushover_app_token
PUSHOVER_USER_KEY=your_pushover_user_key
```

## Broker Routing Logic

### Asset Class Routing (Live Mode)

```
Crypto:
  - 3m, 5m, 15m, 30m → Alpaca or Kraken
  - Larger timeframes → NOT TRADED (crypto excluded from IBKR)

Stocks:
  - 3m, 5m, 15m, 30m → Alpaca
  - 15m, 30m, 1h, 2h, 4h → IBKR

ETFs:
  - 3m, 5m, 15m, 30m → Alpaca
  - 15m, 30m, 1h, 2h, 4h → IBKR

Forex (traditional):
  - All timeframes → FXCM (lower fees)

Forex (crypto exchange):
  - 3m, 5m, 15m, 30m → Kraken

Commodities:
  - 15m, 30m, 1h, 2h, 4h → IBKR

Indices:
  - 15m, 30m, 1h, 2h, 4h → IBKR
```

## Setup Instructions

### 1. Install Required Packages

```bash
pip install alpaca-trade-api
pip install krakenex
pip install fxcmpy
pip install ib_insync
pip install requests  # For LM Studio
```

### 2. Configure API Keys

Edit `.env` file and add your API keys:

**Alpaca**:
1. Sign up at https://alpaca.markets
2. Get API keys from dashboard
3. Add to `.env`: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`

**Kraken**:
1. Sign up at https://www.kraken.com
2. Generate API keys in Settings → API
3. Add to `.env`: `KRAKEN_API_KEY` and `KRAKEN_SECRET`

**FXCM**:
1. Sign up at https://www.fxcm.com
2. Get API token from dashboard
3. Add to `.env`: `FXCM_API_KEY` and `FXCM_ACCESS_TOKEN`

**IBKR**:
1. Already configured
2. Ensure TWS/Gateway is running

### 3. Configure LM Studio

**Step 1: Install LM Studio**
- Download from https://lmstudio.ai
- Install on your machine

**Step 2: Download a Model**
- Open LM Studio
- Go to "Discover" tab
- Search for "llama-3.1-8b-instruct" or similar
- Click "Download"

**Step 3: Start Local Server**
- Go to "Local Server" tab in LM Studio
- Select your downloaded model
- Click "Start Server"
- Default URL: http://localhost:1234/v1

**Step 4: Test Connection**
```bash
curl http://localhost:1234/v1/models
```

**Step 5: Configure in Bot**
- Already configured in `.env`:
  ```bash
  LM_STUDIO_URL=http://localhost:1234/v1
  LM_STUDIO_MODEL=llama-3.1-8b-instruct
  LM_STUDIO_ENABLED=true
  ```

### 4. Adjust Account Balances

Edit `.env` to set your actual account balances:

```bash
ALPACA_ACCOUNT_BALANCE=50.00
KRAKEN_ACCOUNT_BALANCE=50.00
FXCM_ACCOUNT_BALANCE=50.00
IBKR_ACCOUNT_BALANCE=50.00
```

### 5. Adjust Trade Limits

Edit `.env` to change max trades per hour:

```bash
# Global default
MAX_TRADES_PER_HOUR=15

# Per-broker overrides
ALPACA_MAX_TRADES_PER_HOUR=15
KRAKEN_MAX_TRADES_PER_HOUR=15
FXCM_MAX_TRADES_PER_HOUR=15
IBKR_MAX_TRADES_PER_HOUR=15
```

## Testing

### Test Paper Trading (All Brokers, All Assets)

```bash
# Set paper trading mode
export TRADING_MODE=paper

# Run scanner
python bot.py --mode paper --timeframe 15m
```

### Test Live Trading (Strict Routing)

```bash
# Set live trading mode
export TRADING_MODE=live

# Run scanner
python bot.py --mode live --timeframe 15m
```

### Test Specific Broker

```bash
# Test Alpaca only
python bot.py --broker alpaca --timeframe 5m

# Test Kraken only
python bot.py --broker kraken --timeframe 15m

# Test FXCM only
python bot.py --broker fxcm --timeframe 1h

# Test IBKR only
python bot.py --broker ibkr --timeframe 1h
```

## LM Studio Integration

### How It Works

1. **Signal Generation**: Bot generates trading signals
2. **AI Scoring**: Signals sent to LM Studio for confidence scoring
3. **Local Processing**: LM Studio runs on your machine (no external API)
4. **Decision**: High-confidence signals are executed

### Testing LM Studio Connection

```python
# test_lm_studio.py
import requests

url = "http://localhost:1234/v1/chat/completions"
headers = {"Content-Type": "application/json"}
data = {
    "model": "llama-3.1-8b-instruct",
    "messages": [
        {"role": "user", "content": "Is this a good trading signal: BUY AAPL at $175?"}
    ],
    "temperature": 0.7,
    "max_tokens": 100
}

response = requests.post(url, headers=headers, json=data)
print(response.json())
```

### Troubleshooting LM Studio

**Problem**: Connection refused
- **Solution**: Ensure LM Studio server is running
- Check URL: http://localhost:1234/v1

**Problem**: Model not loaded
- **Solution**: Select model in LM Studio and click "Load Model"

**Problem**: Slow responses
- **Solution**: Use smaller model (7B instead of 13B)
- Reduce max_tokens in config

## Monitoring

### Check Broker Status

```bash
# View active brokers
python bot.py --status

# View broker balances
python bot.py --balances

# View trade limits
python bot.py --limits
```

### Logs

- `logs/alpaca.log` - Alpaca trades
- `logs/kraken.log` - Kraken trades
- `logs/fxcm.log` - FXCM trades
- `logs/ibkr.log` - IBKR trades
- `logs/trade_candidates.log` - All trade candidates
- `logs/lm_studio.log` - AI scoring logs

## Fee Comparison

| Broker | Stocks | ETFs | Crypto | Forex | Commodities |
|--------|--------|------|--------|-------|-------------|
| Alpaca | $0 | $0 | Low | N/A | N/A |
| Kraken | N/A | N/A | 0.16% | Low | N/A |
| FXCM | N/A | N/A | N/A | Low | N/A |
| IBKR | $0.005/share | $0.005/share | N/A | Higher | $0.85/contract |

## Summary

✅ **Alpaca**: Stocks, ETFs, Crypto (3m-30m)
✅ **Kraken**: Crypto, Forex-Crypto (3m-30m)
✅ **FXCM**: Forex (all timeframes)
✅ **IBKR**: Everything else (15m-4h, NO crypto)
✅ **LM Studio**: Local AI (no external API needed)
✅ **Adjustable Limits**: 15 trades/hour per broker (configurable)
✅ **Paper Trading**: All brokers, all assets, all timeframes
✅ **Live Trading**: Strict routing based on fees

All brokers have separate account balances and trade limits!
