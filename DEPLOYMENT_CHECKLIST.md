# Trading Bot — Deployment & Setup Checklist

**Status**: Ready for deployment (all code phases complete)

---

## Prerequisites ✅

- [x] Python 3.9+
- [x] Git
- [x] All dependencies installed (`requirements.txt`)
- [x] All code compiles (8/8 core modules verified)

---

## Step 1: Environment Configuration 🔧

### Required Files
- [x] `.env` file exists — update with your credentials
- [x] `.env.template` provided as reference
- [x] `src/config.py` loads all env vars automatically

### Critical Credentials (update `.env`)

#### Database (choose one or both for dual persistence)
```bash
# Supabase (optional, for cloud sync)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key

# SQLite (default, local fallback)
SQLITE_PATH=data/algobot.db
```

#### Broker Credentials (add at least ONE)

**Alpaca** (recommended for paper trading)
```bash
TRADING_MODE=paper                          # paper|live
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_FEED=iex                       # real-time stock/ETF quotes
ALPACA_ACCOUNT_BALANCE=10000.0
```

**Kraken** (crypto)
```bash
KRAKEN_API_KEY=...
KRAKEN_SECRET=...
KRAKEN_ACCOUNT_BALANCE=50.0
```

**IBKR** (stocks, forex, crypto)
```bash
IBKR_HOST=127.0.0.1                        # local TWS/Gateway
IBKR_PORT=7497
IBKR_CLIENT_ID=1
IBKR_ACCOUNT_BALANCE=50000.0
```

**FXCM** (forex)
```bash
FXCM_API_KEY=...
FXCM_ACCESS_TOKEN=...
FXCM_ACCOUNT_TYPE=demo                     # demo|real
FXCM_ACCOUNT_BALANCE=50000.0
```

#### AI Confidence Scorer (choose one or both)

**LM Studio** (free, local inference)
```bash
LM_STUDIO_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=llama-3.1-8b-instruct
# Install: https://lmstudio.ai/
```

**OpenRouter** (cloud, multiple models)
```bash
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=moonshotai/kimi-k2
# Get key: https://openrouter.ai/
```

#### Market Data (optional, Alpaca covers most)

**Finnhub** (stocks, alternative)
```bash
FINNHUB_API_KEY=...
```

**Binance** (crypto alternative)
```bash
BINANCE_API_KEY=...
BINANCE_SECRET=...
```

#### Notifications (optional, highly recommended)

**Pushover** (mobile alerts)
```bash
PUSHOVER_APP_TOKEN=atw...
PUSHOVER_USER_KEY=u...
# Get keys: https://pushover.net/
```

### Risk Parameters (adjust to your account size)

```bash
ACCOUNT_BALANCE=10000                      # Your account size
MAX_RISK_PER_TRADE=0.01                    # 1% per trade
STOP_LOSS_PCT=0.02                         # 2% hard stop
MAX_DAILY_DRAWDOWN=0.10                    # 10% daily kill switch
MAX_TRADES_PER_HOUR=15                     # Rate limit
ML_CONFIDENCE_THRESHOLD=0.65                # ML filter gate
AI_CONFIDENCE_THRESHOLD=0.60                # AI scorer gate
```

### Universe & Regime Settings (optional)

```bash
# Asset universe groups (enable/disable)
UNIVERSE_CORE_CRYPTO_ENABLED=true
UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED=true
UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED=true
UNIVERSE_HIGH_BETA_ETFS_ENABLED=true
UNIVERSE_MEME_COIN_LANE_ENABLED=false

# Regime adaptation
REGIME_MIN_CONFIDENCE=0.40                 # Regime confidence gate
REGIME_ENTRY_FILTER_ENABLED=true
REGIME_SCORE_BIAS_TRENDING=3.0
REGIME_SCORE_BIAS_RANGING=-5.0
```

---

## Step 2: Database Setup 📦

### Initialize SQLite (automatic)
```bash
python -m src.data.db
```
Creates: `data/algobot.db` with all required tables
- `trades` — closed/open positions
- `signals` — BUY/SELL signals + rejections
- `buy_signals` / `sell_signals` — detailed signal metadata
- `ml_features` — training data for ML model
- `regime_snapshots` — regime classification history

### Verify Supabase (if using cloud sync)
- [ ] Create Supabase project
- [ ] Mirror tables to Supabase (schema in `.env`)
- [ ] Test connection before live trading

---

## Step 3: AI Model Setup (choose one) 🤖

### Option A: LM Studio (Recommended for beginners)
```bash
1. Download: https://lmstudio.ai/
2. Install locally
3. Load model: llama-3.1-8b-instruct (7GB VRAM)
4. Start server on http://localhost:1234/v1
5. Test: curl http://localhost:1234/v1/models
```

### Option B: OpenRouter (Cloud, no setup)
```bash
1. Get API key: https://openrouter.ai/
2. Set OPENROUTER_API_KEY in .env
3. No server needed — bot calls remote API
```

### Verify AI Setup
```bash
python -c "from src.ai.lm_studio_client import LMStudioClient; c = LMStudioClient(); print(c.test())"
```

---

## Step 4: Data & Model Initialization 🎯

### Train ML Model (if 200+ historical trades available)
```bash
python -m src.ml.train --status          # Check readiness
python -m src.ml.train --train            # Train XGBoost + LightGBM
```

Outputs:
- `models/xgboost_model.json`
- `models/lightgbm_model.txt`
- `models/ml_scaler.joblib`
- `models/ml_metadata.json`

**Note**: Model will auto-retrain after 50+ new trades or 7+ days.

### Run Backtest (validate setup)
```bash
python bot.py backtest --ticker EURUSD --timeframe 1h --days 30
python bot.py backtest --all --timeframe 4h --days 90
```

---

## Step 5: Paper Trading (required before live) 🧪

### Start Paper Bot (Alpaca)
```bash
# Scan for signals (no execution)
python bot.py scan --all --timeframe 1h

# Start paper trading bot
python bot.py start --timeframe 1h --dry-run

# Or use launch.json for multi-timeframe
python bot.py start --config .claude/launch.json
```

### Monitor 24/7
- Check database: `data/algobot.db`
- View trades: `SELECT * FROM trades ORDER BY entry_time DESC LIMIT 10`
- Check signals: `SELECT * FROM buy_signals WHERE accepted_signal=1 LIMIT 10`

**Paper Trading Checklist**:
- [ ] Can start bot without errors
- [ ] Signals appear in database
- [ ] Regime classification working
- [ ] ML filter filtering correctly
- [ ] No exceptions in logs

---

## Step 6: Live Trading (when confident) 🚀

### Pre-Deployment Verification
```bash
# 1. Verify credentials work
python -c "from src.execution.broker_router import BrokerRouter; b = BrokerRouter(dry_run=False); print('Connected:', b.connect())"

# 2. Test order placement
python bot.py start --timeframe 1h --dry-run  # 1 more full cycle in dry-run

# 3. Check kill switch and limits
python -c "from src.risk.risk_manager import RiskManager; r = RiskManager(); print('Kill switch:', r.is_kill_switch_active())"

# 4. Verify monitoring alerts
# Pushover notification test
```

### Go Live
```bash
# Single timeframe
python bot.py start --timeframe 1h --live

# Multi-timeframe (recommended)
cd .claude && python -m launch.json  # if using launch config

# Or direct:
python bot.py start --timeframe 1h --live &
python bot.py start --timeframe 4h --live &
python bot.py start --timeframe 1d --live &
```

### Live Monitoring
- **Pushover Alerts**: Every signal, trade open/close, kill switch
- **Database**: Real-time trade updates
- **Logs**: `src/notifications/logger.py` stdout
- **Daily Report**: Generated at midnight UTC

---

## Step 7: Continuous Improvement 📈

### Weekly Review
1. Check regime learning suggestions (logged every 100 scans)
2. Review win rate by timeframe and regime
3. Adjust per-asset overrides if needed
4. Rebalance position sizing if account grows

### Monthly Review
1. Analyze PnL by strategy_mode (SCALP, INTERMEDIATE, SWING)
2. Check ML model performance (AUC on validation set)
3. Review correlation guard effectiveness
4. Assess regime detection accuracy

### Quarterly Review
1. Full backtest with latest 3 months data
2. ML model retraining with all historical data
3. Regime learning analysis (cross-regime analytics)
4. Feature engineering review

---

## Troubleshooting 🔧

### Bot Won't Start
```bash
# Check dependencies
pip install -r requirements.txt

# Check config
python -c "from src.config import *; print('Config loaded')"

# Check database
python -m src.data.db

# Check broker credentials
python -c "from src.execution.broker_router import BrokerRouter; print(BrokerRouter().is_ready())"
```

### No Signals Generated
```bash
# Increase lookback (default: 200 candles)
python bot.py scan --all --timeframe 1h --top 20

# Check asset universe
python -c "from src.scanner.asset_universe import get_enabled_symbols; print(get_enabled_symbols())"

# Verify data quality
python -m src.data.market_data --check EURUSD 1h
```

### ML Model Not Training
```bash
# Check trade count
python -m src.ml.train --status

# Manual train (force)
python -m src.ml.train --train --force

# Verify features
python -c "from src.ml.features import N_FEATURES; print(f'{N_FEATURES} features')"
```

### Alerts Not Working
```bash
# Test Pushover
python -c "from src.notifications.pushover import test_notification; test_notification()"

# Verify credentials
python -c "from src.config import PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY; print('Token set:', bool(PUSHOVER_APP_TOKEN))"
```

---

## Deployment Readiness Checklist ✅

### Before Paper Trading
- [ ] `.env` file configured with Alpaca credentials
- [ ] SQLite database created (`data/algobot.db`)
- [ ] AI model available (LM Studio OR OpenRouter)
- [ ] Bot starts without errors
- [ ] Signals appear in database after 1 hour

### Before Live Trading
- [ ] 50+ paper trades completed with good win rate
- [ ] Regime learning suggestions reviewed (logged every 100 scans)
- [ ] All timeframes tested (1h, 4h, 1d)
- [ ] Pushover alerts verified on mobile
- [ ] Account size matches ACCOUNT_BALANCE in config
- [ ] Daily kill switch tested (10% drawdown simulation)
- [ ] Hourly trade cap verified (15 trades/hour)

### Post-Deployment Monitoring
- [ ] Check Pushover alerts 4x daily
- [ ] Weekly review of trade performance
- [ ] Monthly regime learning analysis
- [ ] Quarterly full backtest

---

## All Systems Go ✅

Your trading bot is **production-ready** with:
- ✅ Complete Phase 1, 2, 3 implementation
- ✅ 5 brokers with fallback routing
- ✅ ML auto-retrain + regime learning
- ✅ Portfolio correlation guard
- ✅ Per-asset regime bias tuning
- ✅ Fail-safe risk management
- ✅ Real-time data from 3+ sources
- ✅ Dual persistence (SQLite + Supabase)

**Next Action**: Fill in `.env` with your credentials and run `python bot.py scan --all --timeframe 1h` to see first signals!
