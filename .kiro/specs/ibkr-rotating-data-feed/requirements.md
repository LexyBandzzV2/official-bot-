# Requirements Document

## Introduction

This document specifies requirements for an Interactive Brokers (IBKR) market data integration featuring a professional hedge-fund style rotating data-fetch architecture. The system addresses IBKR's ~100 active ticker subscription limit by implementing a batched historical data approach with selective live streaming, enabling the algorithmic trading bot to monitor large asset universes while maintaining real-time responsiveness for active positions.

## Glossary

- **IBKR_Client**: The Interactive Brokers market data client using ib_insync library
- **Rotating_Scanner**: Component that cycles through asset batches for data collection
- **OHLCV_Cache**: Local storage for recent candlestick data (Open, High, Low, Close, Volume)
- **Historical_Fetcher**: Component that requests historical candle data via reqHistoricalData
- **Live_Stream_Manager**: Component that manages selective reqMktData subscriptions
- **Asset_Universe**: The complete set of symbols to monitor across all asset classes
- **Active_Position**: A trade with an open order or existing market position
- **Signal_Asset**: An asset that has generated a valid trading signal requiring monitoring
- **Batch**: A subset of 20-50 symbols processed together in one rotation cycle
- **Indicator_Layer**: The calculation layer for Alligator, Vortex, and Stochastic indicators
- **Connection_Manager**: Component handling IBKR socket connection lifecycle and reconnection
- **Asset_Class**: Category of tradable instrument (Stock, Forex, Crypto, Future)

## Requirements

### Requirement 1: IBKR Connection Management

**User Story:** As a trading bot operator, I want reliable IBKR connectivity with automatic reconnection, so that temporary network issues do not halt market data collection.

#### Acceptance Criteria

1. THE IBKR_Client SHALL establish connection to IBKR TWS or Gateway using ib_insync library
2. WHEN the IBKR socket connection is lost, THEN THE Connection_Manager SHALL attempt reconnection with exponential backoff up to 5 attempts
3. WHEN reconnection succeeds, THE Connection_Manager SHALL restore all active subscriptions that existed before disconnection
4. THE IBKR_Client SHALL log all connection state changes with timestamps
5. WHEN connection attempts fail after 5 retries, THEN THE Connection_Manager SHALL notify the operator and enter degraded mode

### Requirement 2: Historical Data Fetching

**User Story:** As a trading system, I want to fetch historical OHLCV candles in batches, so that indicators can calculate on recent market data without maintaining persistent streams.

#### Acceptance Criteria

1. THE Historical_Fetcher SHALL request historical candle data using IBKR reqHistoricalData API
2. WHEN fetching historical data, THE Historical_Fetcher SHALL request between 50 and 200 candles per symbol based on indicator requirements
3. THE Historical_Fetcher SHALL support multiple timeframes including 1m, 5m, 15m, 30m, 1h, 4h, and 1d
4. THE Historical_Fetcher SHALL support all four asset classes: Stock, Forex, Crypto, and Future
5. WHEN IBKR rate limits are encountered, THEN THE Historical_Fetcher SHALL queue requests and retry with appropriate delays
6. THE Historical_Fetcher SHALL execute all data requests asynchronously without blocking indicator calculations
7. WHEN historical data is received, THE Historical_Fetcher SHALL normalize it to standard OHLCV format with UTC timestamps

### Requirement 3: Local OHLCV Cache

**User Story:** As an indicator calculation system, I want locally cached OHLCV data, so that I can compute technical indicators without waiting for network requests.

#### Acceptance Criteria

1. THE OHLCV_Cache SHALL store recent candle data for each symbol-timeframe pair in memory
2. THE OHLCV_Cache SHALL maintain at least 200 candles per symbol-timeframe pair to support indicator lookback periods
3. WHEN new candle data arrives, THE OHLCV_Cache SHALL append it and remove candles older than the retention window
4. THE OHLCV_Cache SHALL provide thread-safe read access for concurrent indicator calculations
5. WHEN the cache is queried for a symbol-timeframe pair that does not exist, THE OHLCV_Cache SHALL return an empty DataFrame
6. THE OHLCV_Cache SHALL expose a method to retrieve cached data as a pandas DataFrame with columns: time, open, high, low, close, volume

### Requirement 4: Rotating Market Scanner

**User Story:** As a market monitoring system, I want to rotate through the asset universe in batches, so that I can monitor hundreds of symbols without exceeding IBKR's subscription limits.

#### Acceptance Criteria

1. THE Rotating_Scanner SHALL divide the Asset_Universe into batches of 20 to 50 symbols
2. THE Rotating_Scanner SHALL process one batch at a time in sequential rotation
3. WHEN processing a batch, THE Rotating_Scanner SHALL fetch historical data for all symbols in the batch
4. WHEN a batch completes processing, THE Rotating_Scanner SHALL move to the next batch after a configurable delay of 5 to 30 seconds
5. THE Rotating_Scanner SHALL complete one full rotation through all batches within a configurable cycle time of 5 to 15 minutes
6. THE Rotating_Scanner SHALL skip symbols that have active live streams to avoid duplicate subscriptions
7. WHEN the final batch completes, THE Rotating_Scanner SHALL restart from the first batch

### Requirement 5: Selective Live Stream Management

**User Story:** As a position manager, I want live market data streams for assets with active positions or signals, so that I can respond immediately to price movements without wasting subscription slots.

#### Acceptance Criteria

1. WHEN a valid trading signal is generated for an asset, THE Live_Stream_Manager SHALL subscribe to live reqMktData for that symbol
2. WHEN a trade is opened for an asset, THE Live_Stream_Manager SHALL ensure a live reqMktData subscription exists for that symbol
3. WHEN a trade is closed and no other active signals exist for that asset, THE Live_Stream_Manager SHALL unsubscribe from reqMktData for that symbol
4. THE Live_Stream_Manager SHALL maintain a maximum of 80 concurrent live subscriptions to stay within IBKR limits
5. WHEN the subscription limit is reached and a new subscription is required, THE Live_Stream_Manager SHALL unsubscribe from the oldest non-position asset
6. WHEN live tick data is received, THE Live_Stream_Manager SHALL update the OHLCV_Cache with the latest price information
7. THE Live_Stream_Manager SHALL track subscription timestamps to support age-based eviction

### Requirement 6: Integration with Indicator Layer

**User Story:** As an indicator calculation system, I want to receive OHLCV data from the IBKR feed, so that Alligator, Vortex, and Stochastic indicators can generate signals.

#### Acceptance Criteria

1. THE IBKR_Client SHALL provide OHLCV data in the same format as existing market data sources (Kraken, Coinbase, NDAX)
2. THE IBKR_Client SHALL expose a method compatible with get_latest_candles(symbol, timeframe, count) interface
3. WHEN the Indicator_Layer requests candle data, THE IBKR_Client SHALL return data from OHLCV_Cache if available
4. WHEN cached data is insufficient or stale, THE IBKR_Client SHALL trigger a historical data fetch and return cached data after update
5. THE IBKR_Client SHALL ensure all returned DataFrames include columns: time, open, high, low, close, volume
6. THE IBKR_Client SHALL convert all timestamps to UTC timezone-aware datetime objects

### Requirement 7: Signal Engine Integration

**User Story:** As a signal generation system, I want IBKR market data to flow into the existing signal engine, so that trading signals can be generated from IBKR-sourced assets.

#### Acceptance Criteria

1. THE IBKR_Client SHALL register as a data source option in the market_data module
2. WHEN the signal engine requests data for an IBKR-sourced symbol, THE IBKR_Client SHALL provide OHLCV candles
3. THE IBKR_Client SHALL support the source parameter in get_historical_ohlcv() and get_latest_candles() functions
4. WHEN IBKR is specified as the source, THE market_data module SHALL route requests to IBKR_Client
5. THE IBKR_Client SHALL maintain compatibility with the SignalEngine.evaluate() method signature

### Requirement 8: Execution Layer Integration

**User Story:** As a trade execution system, I want the IBKR market data client to coordinate with the existing IBKR execution adapter, so that market data and order execution share the same connection.

#### Acceptance Criteria

1. THE IBKR_Client SHALL share the same ib_insync IB connection instance with IBKRAdapter
2. THE IBKR_Client SHALL coordinate connection lifecycle with IBKRAdapter to prevent conflicts
3. WHEN IBKRAdapter places an order, THE IBKR_Client SHALL not interfere with order execution
4. THE IBKR_Client SHALL use a separate client ID from IBKRAdapter if connection sharing is not feasible
5. THE IBKR_Client SHALL expose connection status to both market data and execution components

### Requirement 9: Multi-Timeframe Support

**User Story:** As a multi-timeframe trading system, I want to fetch and cache OHLCV data for multiple timeframes simultaneously, so that different strategies can operate on different time horizons.

#### Acceptance Criteria

1. THE OHLCV_Cache SHALL maintain separate data stores for each symbol-timeframe combination
2. THE Rotating_Scanner SHALL support scanning multiple timeframes in parallel or sequential mode
3. WHEN multiple timeframes are configured, THE Historical_Fetcher SHALL batch requests by symbol to minimize API calls
4. THE IBKR_Client SHALL map standard timeframe strings (1m, 5m, 15m, 30m, 1h, 4h, 1d) to IBKR duration and bar size parameters
5. WHEN a timeframe is not supported by IBKR, THE IBKR_Client SHALL log a warning and skip that timeframe

### Requirement 10: Asset Class Contract Resolution

**User Story:** As a multi-asset trading system, I want automatic IBKR contract resolution, so that symbols are correctly mapped to Stock, Forex, Crypto, or Future contracts.

#### Acceptance Criteria

1. THE IBKR_Client SHALL resolve canonical symbols to appropriate IBKR Contract objects (Stock, Forex, Crypto, Future)
2. WHEN resolving a forex symbol, THE IBKR_Client SHALL create a Forex contract with the correct currency pair
3. WHEN resolving a stock symbol, THE IBKR_Client SHALL create a Stock contract with exchange set to SMART and currency USD
4. WHEN resolving a crypto symbol, THE IBKR_Client SHALL create a Crypto contract with appropriate exchange (e.g., PAXOS)
5. WHEN resolving a futures symbol, THE IBKR_Client SHALL create a Future contract with correct symbol and expiration
6. WHEN contract resolution fails, THE IBKR_Client SHALL log the failure and skip that symbol in the current rotation
7. THE IBKR_Client SHALL use the existing get_asset_class() function from symbol_mapper for classification

### Requirement 11: Rate Limit Compliance

**User Story:** As an IBKR API consumer, I want automatic rate limit handling, so that the bot does not trigger IBKR pacing violations or account restrictions.

#### Acceptance Criteria

1. THE Historical_Fetcher SHALL enforce a minimum delay of 1 second between consecutive reqHistoricalData requests
2. THE Historical_Fetcher SHALL track request counts per 10-minute window and limit to 60 requests per window
3. WHEN approaching rate limits, THE Historical_Fetcher SHALL increase delays between requests
4. WHEN IBKR returns a pacing violation error, THE Historical_Fetcher SHALL pause requests for 60 seconds before resuming
5. THE Live_Stream_Manager SHALL enforce a minimum delay of 100ms between subscription changes
6. THE IBKR_Client SHALL log all rate limit events with severity level WARNING

### Requirement 12: Data Quality Validation

**User Story:** As an indicator calculation system, I want validated OHLCV data, so that technical indicators produce accurate signals without garbage input.

#### Acceptance Criteria

1. WHEN OHLCV data is received, THE IBKR_Client SHALL validate that high >= low for every candle
2. WHEN OHLCV data is received, THE IBKR_Client SHALL validate that high >= open and high >= close for every candle
3. WHEN OHLCV data is received, THE IBKR_Client SHALL validate that low <= open and low <= close for every candle
4. WHEN validation fails for a candle, THE IBKR_Client SHALL log the invalid data and exclude that candle from the cache
5. THE IBKR_Client SHALL reject candles with negative or zero prices
6. THE IBKR_Client SHALL reject candles with timestamps in the future
7. WHEN duplicate candles are received for the same timestamp, THE IBKR_Client SHALL keep the most recent version

### Requirement 13: Configuration Management

**User Story:** As a bot operator, I want configurable parameters for the rotating scanner, so that I can tune performance based on asset universe size and IBKR account limits.

#### Acceptance Criteria

1. THE IBKR_Client SHALL read configuration from environment variables or config file
2. THE configuration SHALL include: batch_size (default 30), rotation_delay_seconds (default 10), max_live_subscriptions (default 80)
3. THE configuration SHALL include: historical_candle_count (default 200), cache_retention_hours (default 24)
4. THE configuration SHALL include: rate_limit_requests_per_10min (default 60), reconnect_max_attempts (default 5)
5. WHEN configuration values are invalid or missing, THE IBKR_Client SHALL use documented default values
6. THE IBKR_Client SHALL log all configuration values at startup

### Requirement 14: Error Handling and Logging

**User Story:** As a bot operator, I want comprehensive error logging, so that I can diagnose issues with IBKR connectivity and data quality.

#### Acceptance Criteria

1. WHEN any IBKR API error occurs, THE IBKR_Client SHALL log the error code, message, and affected symbol
2. THE IBKR_Client SHALL log INFO level messages for: connection established, batch rotation started, subscription changes
3. THE IBKR_Client SHALL log WARNING level messages for: rate limit approaches, reconnection attempts, data validation failures
4. THE IBKR_Client SHALL log ERROR level messages for: connection failures, API errors, contract resolution failures
5. WHEN an exception occurs during data processing, THE IBKR_Client SHALL log the full stack trace and continue processing other symbols
6. THE IBKR_Client SHALL include symbol, timeframe, and timestamp context in all log messages

### Requirement 15: Market Scanner Integration

**User Story:** As a market scanning system, I want IBKR data to feed into the existing market scanner, so that the bot can discover trading opportunities across IBKR-sourced assets.

#### Acceptance Criteria

1. THE IBKR_Client SHALL provide data to MarketScanner when IBKR is configured as the data source
2. WHEN MarketScanner requests candidate ranking, THE IBKR_Client SHALL provide current cached data for scoring
3. THE Rotating_Scanner SHALL prioritize symbols with higher candidate scores in subsequent rotations
4. WHEN a symbol generates a valid signal, THE Rotating_Scanner SHALL increase that symbol's scan frequency
5. THE IBKR_Client SHALL support the existing best_source() function for automatic source selection

### Requirement 16: Performance Monitoring

**User Story:** As a system operator, I want performance metrics for the IBKR data feed, so that I can identify bottlenecks and optimize rotation parameters.

#### Acceptance Criteria

1. THE IBKR_Client SHALL track and expose metrics: total_requests, failed_requests, average_response_time_ms, cache_hit_rate
2. THE IBKR_Client SHALL track rotation metrics: batches_per_hour, symbols_per_minute, full_rotation_time_seconds
3. THE IBKR_Client SHALL track subscription metrics: active_live_streams, subscription_changes_per_hour
4. WHEN requested, THE IBKR_Client SHALL return current metrics as a dictionary
5. THE IBKR_Client SHALL reset counters for rate-based metrics every hour
6. THE IBKR_Client SHALL log performance summary every 60 minutes at INFO level

### Requirement 17: Graceful Degradation

**User Story:** As a trading system, I want graceful degradation when IBKR data is unavailable, so that the bot can continue operating with alternative data sources.

#### Acceptance Criteria

1. WHEN IBKR connection fails and cannot be restored, THE IBKR_Client SHALL mark itself as unavailable
2. WHEN IBKR_Client is unavailable, THE market_data module SHALL fall back to alternative sources (CCXT, Finnhub, yfinance)
3. WHEN switching to fallback sources, THE system SHALL log the source change at WARNING level
4. THE IBKR_Client SHALL periodically attempt to restore connection while in degraded mode
5. WHEN IBKR connection is restored, THE IBKR_Client SHALL resume normal operation and log the recovery

### Requirement 18: Cache Persistence

**User Story:** As a trading bot, I want OHLCV cache persistence across restarts, so that I don't need to re-fetch all historical data when the bot restarts.

#### Acceptance Criteria

1. WHEN the IBKR_Client shuts down, THE OHLCV_Cache SHALL serialize cached data to disk in the data/historical/ directory
2. WHEN the IBKR_Client starts up, THE OHLCV_Cache SHALL load previously cached data from disk if available
3. THE OHLCV_Cache SHALL store data in parquet format with filename pattern: {symbol}_{timeframe}_ibkr.parquet
4. WHEN loading cached data, THE OHLCV_Cache SHALL validate timestamps and discard stale data older than 7 days
5. THE OHLCV_Cache SHALL handle missing or corrupted cache files gracefully by starting with empty cache

### Requirement 19: Live Stream Data Integration

**User Story:** As a position monitoring system, I want live tick updates integrated into cached candles, so that stop losses and trailing stops react to real-time price movements.

#### Acceptance Criteria

1. WHEN live tick data is received via reqMktData, THE Live_Stream_Manager SHALL update the most recent candle in OHLCV_Cache
2. THE Live_Stream_Manager SHALL update the close price of the current candle with each new tick
3. THE Live_Stream_Manager SHALL update the high price if the tick price exceeds the current candle high
4. THE Live_Stream_Manager SHALL update the low price if the tick price is below the current candle low
5. WHEN a new candle period begins, THE Live_Stream_Manager SHALL create a new candle row in the cache
6. THE Live_Stream_Manager SHALL accumulate volume from ticks into the current candle volume

### Requirement 20: Batch Priority Queue

**User Story:** As a signal-driven system, I want priority-based batch scheduling, so that assets with recent signals or open positions are scanned more frequently.

#### Acceptance Criteria

1. THE Rotating_Scanner SHALL maintain a priority queue where each symbol has a priority score
2. WHEN forming batches, THE Rotating_Scanner SHALL include higher priority symbols more frequently
3. WHEN an asset has an Active_Position, THE Rotating_Scanner SHALL assign it maximum priority
4. WHEN an asset is a Signal_Asset, THE Rotating_Scanner SHALL assign it high priority
5. WHEN an asset has no recent activity, THE Rotating_Scanner SHALL assign it normal priority
6. THE Rotating_Scanner SHALL recalculate priorities after each full rotation cycle

### Requirement 21: Symbol Mapper Integration

**User Story:** As a multi-source data system, I want IBKR symbols registered in the symbol mapper, so that the bot can route data requests to IBKR appropriately.

#### Acceptance Criteria

1. THE IBKR_Client SHALL register supported symbols in the symbol_mapper module
2. WHEN a symbol is available from both IBKR and other sources, THE symbol_mapper SHALL prefer IBKR for stocks and futures
3. THE symbol_mapper SHALL expose an ibkr_supported(symbol) function that returns True if IBKR can provide data for that symbol
4. THE IBKR_Client SHALL provide a method to query which asset classes are supported
5. WHEN best_source() is called for a symbol, THE symbol_mapper SHALL consider IBKR availability in source selection

### Requirement 22: Asynchronous Architecture

**User Story:** As a real-time trading system, I want non-blocking data fetching, so that slow historical data requests do not delay signal generation or order execution.

#### Acceptance Criteria

1. THE Historical_Fetcher SHALL execute all reqHistoricalData calls asynchronously using asyncio
2. THE IBKR_Client SHALL provide both synchronous and asynchronous interfaces for data retrieval
3. WHEN multiple symbols require data updates, THE Historical_Fetcher SHALL fetch them concurrently up to a limit of 10 parallel requests
4. THE IBKR_Client SHALL use asyncio event loops compatible with ib_insync's event-driven architecture
5. WHEN a data request times out after 30 seconds, THE Historical_Fetcher SHALL cancel the request and log a timeout error

### Requirement 23: Startup Initialization

**User Story:** As a trading bot, I want fast startup with progressive data loading, so that the bot can begin monitoring markets quickly without waiting for full cache population.

#### Acceptance Criteria

1. WHEN the IBKR_Client starts, THE system SHALL establish connection before loading cache data
2. THE IBKR_Client SHALL load persisted cache data asynchronously in the background
3. THE IBKR_Client SHALL begin rotating through batches immediately after connection, even if cache is not fully loaded
4. WHEN cache loading completes, THE IBKR_Client SHALL log the number of symbols and candles loaded
5. THE IBKR_Client SHALL complete startup initialization within 10 seconds excluding cache loading

### Requirement 24: Health Monitoring

**User Story:** As a system operator, I want health status reporting for the IBKR data feed, so that I can verify the system is operating correctly.

#### Acceptance Criteria

1. THE IBKR_Client SHALL expose a get_health_status() method that returns connection state, active subscriptions, and cache statistics
2. THE health status SHALL include: is_connected, last_successful_fetch_time, active_live_streams_count, cached_symbols_count
3. THE health status SHALL include: current_batch_index, rotation_cycle_count, failed_requests_last_hour
4. WHEN the bot status command is invoked, THE system SHALL display IBKR health metrics
5. THE IBKR_Client SHALL mark health status as degraded when connection is lost or error rate exceeds 10% over 10 minutes

### Requirement 25: Testing and Validation

**User Story:** As a developer, I want comprehensive testing for the IBKR data feed, so that I can verify correctness before deploying to live trading.

#### Acceptance Criteria

1. THE IBKR_Client SHALL include unit tests for contract resolution across all asset classes
2. THE IBKR_Client SHALL include integration tests that verify data fetching with IBKR paper trading account
3. THE OHLCV_Cache SHALL include property-based tests that verify cache invariants: data is sorted by time, no duplicate timestamps, all candles are valid
4. THE Historical_Fetcher SHALL include tests for rate limit handling and retry logic
5. THE Live_Stream_Manager SHALL include tests for subscription lifecycle and limit enforcement
6. FOR ALL valid OHLCV DataFrames, caching then retrieving SHALL return equivalent data (round-trip property)
