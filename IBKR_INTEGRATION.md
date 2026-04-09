# IBKR Rotating Data Feed Integration

## Overview

This document describes the Interactive Brokers (IBKR) market data integration for the trading bot. The implementation uses a professional hedge-fund style rotating batch architecture to respect IBKR's ~100 active ticker subscription limit.

## Architecture

### Core Components

1. **ConnectionManager** (`src/data/ibkr/connection_manager.py`)
   - Manages IBKR socket connection via ib_insync
   - Implements exponential backoff reconnection (max 5 attempts)
   - Handles subscription restoration after reconnection

2. **ContractResolver** (`src/data/ibkr/contract_resolver.py`)
   - Maps canonical symbols to IBKR Contract objects
   - Supports Stock, Forex, Crypto, Future asset classes
   - Caches contract resolutions for performance

3. **OHLCVCache** (`src/data/ibkr/cache.py`)
   - In-memory OHLCV data storage with thread-safe access
   - Disk persistence using parquet format
   - Live tick updates for current candle
   - Automatic pruning (24-hour retention, min 200 candles)

4. **RateLimiter** (`src/data/ibkr/rate_limiter.py`)
   - Enforces 60 requests per 10-minute window
   - Minimum 1-second delay between requests
   - Pacing violation handling (60-second pause)

5. **HistoricalFetcher** (`src/data/ibkr/historical_fetcher.py`)
   - Async historical data requests via reqHistoricalData
   - Supports all timeframes (1m, 5m, 15m, 30m, 1h, 4h, 1d)
   - Data validation (OHLC consistency, no future timestamps)
   - Retry logic with exponential backoff

6. **LiveStreamManager** (`src/data/ibkr/live_stream_manager.py`)
   - Selective reqMktData subscriptions (80 concurrent limit)
   - Priority-based eviction (position > signal > manual > scanner)
   - Tick data processing and cache updates

7. **RotatingScanner** (`src/data/ibkr/rotating_scanner.py`)
   - Batch rotation through asset universe (20-50 symbols per batch)
   - Priority-based scheduling (positions and signals get higher priority)
   - Skips symbols with active live streams
   - Configurable rotation cycle (5-15 minutes)

8. **IBKRMarketDataClient** (`src/data/ibkr/client.py`)
   - Main facade coordinating all components
   - Cache-first data retrieval
   - Health and metrics reporting

### Data Flow

```
Market Universe
    ↓
Batched Historical Data Fetch (HistoricalFetcher)
    ↓
Local OHLCV Cache (OHLCVCache)
    ↓
Indicator Calculation Layer (Alligator, Vortex, Stochastic)
    ↓
Signal Engine (SignalEngine)
    ↓
Execution Engine (IBKRAdapter)
```

### Connection Coordination

- **Market Data Client**: Uses base `IBKR_CLIENT_ID` from config
- **Execution Adapter**: Uses `IBKR_CLIENT_ID + 1` to avoid conflicts
- Both can share the same TWS/Gateway instance
- Position-triggered subscriptions via `PositionCoordinator`

## Configuration

### Environment Variables

Add to `.env`:

```bash
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7497              # 7497 for TWS paper, 7496 for TWS live, 4002 for Gateway paper, 4001 for Gateway live
IBKR_CLIENT_ID=1            # Base client ID (execution uses +1)

# IBKR Data Feed Settings
IBKR_BATCH_SIZE=30                    # Symbols per batch (20-50)
IBKR_ROTATION_DELAY_SECONDS=10        # Delay between batches (5-30)
IBKR_ROTATION_CYCLE_MINUTES=10        # Full rotation cycle time (5-15)
IBKR_MAX_LIVE_SUBSCRIPTIONS=80        # Max concurrent live streams (80-100)
IBKR_CACHE_RETENTION_HOURS=24         # Cache retention period
IBKR_CACHE_PERSIST_PATH=data/ibkr_cache  # Cache persistence directory
```

### TWS/Gateway Setup

1. **Install TWS or IB Gateway**
   - Download from Interactive Brokers website
   - Use paper trading account for testing

2. **Enable API Access**
   - TWS: Edit → Global Configuration → API → Settings
   - Enable "Enable ActiveX and Socket Clients"
   - Set "Socket port" to 7497 (paper) or 7496 (live)
   - Add 127.0.0.1 to "Trusted IP Addresses"
   - Disable "Read-Only API"

3. **Start TWS/Gateway**
   - Log in with paper trading credentials
   - Keep TWS/Gateway running while bot is active

## Usage

### Basic Usage

```python
from src.data.market_data import get_latest_candles, get_historical_ohlcv

# Fetch latest candles from IBKR
df = get_latest_candles("AAPL", "1h", count=200, source="ibkr")

# Fetch historical data from IBKR
from datetime import datetime, timedelta, timezone
start = datetime.now(timezone.utc) - timedelta(days=7)
end = datetime.now(timezone.utc)
df = get_historical_ohlcv("EURUSD", "1h", start, end, source="ibkr")
```

### Scanner Integration

```python
from src.scanner.market_scanner import MarketScanner

# Create scanner with IBKR as data source
scanner = MarketScanner(
    symbols=["AAPL", "TSLA", "EURUSD", "BTCUSD"],
    timeframe="1h",
    top_candidates=5,
    dry_run=True,
    data_source="ibkr"  # Force IBKR as data source
)

# Run scanner
scanner.start()
```

### Position-Triggered Subscriptions

```python
from src.data.ibkr.position_coordinator import PositionCoordinator
from src.data.market_data import _get_ibkr_client
from src.execution.ibkr_adapter import IBKRAdapter

# Initialize components
market_data_client = _get_ibkr_client()
execution_adapter = IBKRAdapter()

# Create coordinator
coordinator = PositionCoordinator(market_data_client, execution_adapter)

# Coordinator automatically subscribes to live data when positions open
# and unsubscribes when positions close
```

## Testing

### Validation Script

Test IBKR data compatibility with signal engine:

```bash
python validate_ibkr_signals.py
```

This script:
- Fetches data from IBKR for test symbols
- Compares with existing data sources
- Tests indicator calculations
- Validates SignalEngine integration

### Small Watchlist Test

Test with a small watchlist before scaling:

```bash
python test_ibkr_small_watchlist.py
```

This script:
- Tests 5-10 symbols across different asset classes
- Validates data fetching, signal generation, and logging
- Provides detailed output for debugging

### Unit Tests

Run unit tests for IBKR components:

```bash
pytest tests/test_connection_manager.py
pytest tests/test_cache.py
pytest tests/test_client.py
```

## Trade Candidate Logging

All trade candidates are logged with comprehensive details to `logs/trade_candidates.log`:

- Symbol
- Timeframe
- Candle timestamp
- Indicator values (Alligator jaw/teeth/lips, Vortex vi+/vi-, Stochastic k/d)
- Signal direction (BUY/SELL)
- Entry price
- Stop loss
- Exit condition
- ML confidence score
- AI confidence score
- Whether trade was sent to IBKR
- Rejection reason (if rejected)

Example log entry:

```
2026-03-29 14:30:00 | CANDIDATE | symbol=AAPL | timeframe=1h | candle_time=2026-03-29 14:00:00 | direction=BUY | entry=175.2500 | stop_loss=173.1200 | exit_condition=lips_touch_teeth | alligator=[jaw=174.5000, teeth=174.8000, lips=175.1000] | vortex=[vi+=1.0500, vi-=0.9500] | stochastic=[k=82.50, d=78.30] | ml_conf=85.00% | ai_conf=78.00% | status=SENT_TO_IBKR
```

## Monitoring

### Health Status

```python
from src.data.market_data import _get_ibkr_client

client = _get_ibkr_client()
health = client.get_health_status()

print(f"Connected: {health['is_connected']}")
print(f"Active live streams: {health['active_live_streams']}")
print(f"Cached symbols: {health['cached_symbols']}")
print(f"Error rate: {health['error_rate_percent']}%")
```

### Performance Metrics

```python
metrics = client.get_metrics()

print(f"Total requests: {metrics['total_requests']}")
print(f"Failed requests: {metrics['failed_requests']}")
print(f"Average response time: {metrics['average_response_time_ms']}ms")
print(f"Cache hit rate: {metrics['cache_hit_rate']:.2%}")
print(f"Batches per hour: {metrics['batches_per_hour']}")
```

## Troubleshooting

### Connection Issues

**Problem**: Cannot connect to IBKR

**Solutions**:
1. Verify TWS/Gateway is running
2. Check port number in `.env` matches TWS/Gateway settings
3. Verify API access is enabled in TWS/Gateway
4. Check firewall settings
5. Ensure client ID is not already in use

### Rate Limit Errors

**Problem**: Pacing violations or rate limit errors

**Solutions**:
1. Reduce `IBKR_BATCH_SIZE` in config
2. Increase `IBKR_ROTATION_DELAY_SECONDS`
3. Check for duplicate requests
4. Monitor rate limiter stats

### Data Quality Issues

**Problem**: Missing or invalid candles

**Solutions**:
1. Check symbol is supported by IBKR
2. Verify market hours (some assets only trade during specific hours)
3. Check data validation logs for rejected candles
4. Ensure sufficient historical data is available

### Cache Issues

**Problem**: Stale or missing cached data

**Solutions**:
1. Clear cache directory: `rm -rf data/ibkr_cache/*`
2. Restart bot to reload cache
3. Check cache retention settings
4. Monitor cache statistics

## Performance Optimization

### Batch Size Tuning

- **Small batches (20-30)**: Lower rate limit risk, slower full rotation
- **Large batches (40-50)**: Faster full rotation, higher rate limit risk
- **Recommended**: Start with 30, adjust based on rate limit violations

### Rotation Cycle Tuning

- **Fast cycles (5-7 min)**: More frequent updates, higher API load
- **Slow cycles (10-15 min)**: Less API load, less frequent updates
- **Recommended**: 10 minutes for most use cases

### Live Stream Management

- **Conservative (60-70)**: Lower risk of hitting subscription limit
- **Aggressive (80-90)**: Maximum live data coverage
- **Recommended**: 80 for balanced performance

## Integration Checklist

- [x] IBKR data source registered in market_data module
- [x] SignalEngine compatibility verified
- [x] Trade candidate logging implemented
- [x] IBKRAdapter connection coordination
- [x] Position-triggered subscriptions
- [x] MarketScanner integration
- [x] Symbol mapper IBKR support
- [x] Validation scripts created
- [x] Small watchlist test script created
- [ ] Full universe testing
- [ ] Production deployment

## Next Steps

1. **Test with small watchlist** (5-10 symbols)
   ```bash
   python test_ibkr_small_watchlist.py
   ```

2. **Validate candle accuracy** against existing sources
   ```bash
   python validate_ibkr_signals.py
   ```

3. **Confirm indicator signals** match expected output
   - Review logs/trade_candidates.log
   - Compare Alligator, Vortex, Stochastic values

4. **Test signal engine integration**
   - Verify signals generate correctly
   - Check entry/stop loss calculations

5. **Scale to full universe** (if tests pass)
   - Update scanner symbol list
   - Monitor performance metrics
   - Watch for rate limit violations

6. **Enable execution** (after validation)
   - Keep paper trading mode
   - Monitor trade execution
   - Verify position-triggered subscriptions

## Support

For issues or questions:
1. Check logs in `logs/` directory
2. Review health status and metrics
3. Consult IBKR API documentation
4. Check ib_insync documentation

## References

- [IBKR API Documentation](https://interactivebrokers.github.io/tws-api/)
- [ib_insync Documentation](https://ib-insync.readthedocs.io/)
- [IBKR Paper Trading](https://www.interactivebrokers.com/en/index.php?f=1286)
