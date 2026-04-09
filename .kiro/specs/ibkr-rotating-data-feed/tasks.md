# Implementation Plan: IBKR Rotating Data Feed

## Overview

This implementation plan breaks down the IBKR rotating data feed feature into discrete coding tasks. The system implements a professional hedge-fund style rotating batch architecture for Interactive Brokers market data integration, addressing IBKR's ~100 active ticker subscription limit through batched historical data fetching with selective live streaming.

The implementation follows a bottom-up approach: foundational components first (connection, contracts, cache), then data fetching layers (historical and live), followed by coordination components (rotating scanner), and finally integration with existing bot systems.

## Tasks

- [ ] 1. Set up project structure and configuration
  - Create `src/data/ibkr/` directory for IBKR-specific components
  - Create `IBKRConfig` dataclass in `src/data/ibkr/config.py` with all configuration parameters
  - Add IBKR configuration loading from environment variables
  - Update `.env.template` with IBKR configuration variables
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_

- [ ] 2. Implement ConnectionManager
  - [ ] 2.1 Create ConnectionManager class with ib_insync integration
    - Implement `connect()` method to establish IBKR connection
    - Implement `is_connected()` status check
    - Implement `get_ib_instance()` to return shared IB instance
    - Add connection state logging
    - _Requirements: 1.1, 1.4_

  - [ ] 2.2 Implement reconnection logic with exponential backoff
    - Implement `reconnect()` method with max 5 attempts
    - Add exponential backoff timing (base 2.0)
    - Implement degraded mode after max retries
    - Add disconnect handler registration
    - _Requirements: 1.2, 1.5_

  - [ ] 2.3 Implement subscription restoration after reconnection
    - Track active subscriptions before disconnect
    - Restore subscriptions on successful reconnection
    - Log restoration success/failures
    - _Requirements: 1.3_

  - [ ]* 2.4 Write property test for reconnection exponential backoff
    - **Property 1: Reconnection Exponential Backoff**
    - **Validates: Requirements 1.2**

  - [ ]* 2.5 Write property test for subscription restoration
    - **Property 2: Subscription Restoration After Reconnection**
    - **Validates: Requirements 1.3**

  - [ ]* 2.6 Write property test for connection state logging
    - **Property 3: Connection State Logging**
    - **Validates: Requirements 1.4**

- [ ] 3. Implement ContractResolver
  - [ ] 3.1 Create ContractResolver class with contract resolution logic
    - Implement `resolve()` method for symbol to Contract mapping
    - Support Stock, Forex, Crypto, Future contract types
    - Integrate with existing `get_asset_class()` from symbol_mapper
    - Add contract resolution caching
    - Handle resolution failures gracefully
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [ ] 3.2 Implement contract details retrieval and cache management
    - Implement `get_contract_details()` method
    - Implement `clear_cache()` method
    - Add logging for resolution failures
    - _Requirements: 10.6_

  - [ ]* 3.3 Write unit tests for contract resolution
    - Test all four asset classes (Stock, Forex, Crypto, Future)
    - Test invalid symbol handling
    - Test cache behavior
    - _Requirements: 25.1_

- [ ] 4. Implement OHLCVCache
  - [ ] 4.1 Create OHLCVCache class with in-memory storage
    - Implement internal storage structure (Dict[Tuple[str, str], pd.DataFrame])
    - Implement `get()` method for cache retrieval
    - Implement `update()` method for appending candles
    - Implement thread-safe access with locks
    - Return empty DataFrame for cache misses
    - _Requirements: 3.1, 3.4, 3.5, 3.6_

  - [ ] 4.2 Implement cache pruning and retention logic
    - Implement retention window enforcement (24 hours default)
    - Prune old candles on update
    - Maintain minimum 200 candles per symbol-timeframe
    - _Requirements: 3.2, 3.3_

  - [ ] 4.3 Implement current candle updates for live tick data
    - Implement `update_current_candle()` method
    - Update close, high, low, volume from tick data
    - Handle new candle period creation
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6_

  - [ ] 4.4 Implement cache persistence to disk
    - Implement `persist()` method to save cache as parquet files
    - Implement `load()` method to restore cache from disk
    - Use filename pattern: {symbol}_{timeframe}_ibkr.parquet
    - Handle corrupted files gracefully
    - Prune stale data (>7 days) on load
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [ ] 4.5 Implement cache statistics and monitoring
    - Implement `get_cache_stats()` method
    - Track symbols count, candles count, memory usage
    - _Requirements: 16.1, 24.2_

  - [ ]* 4.6 Write property test for cache retention capacity
    - **Property 7: Cache Retention Capacity**
    - **Validates: Requirements 3.2**

  - [ ]* 4.7 Write property test for cache pruning on update
    - **Property 8: Cache Pruning on Update**
    - **Validates: Requirements 3.3**

  - [ ]* 4.8 Write property test for thread-safe cache access
    - **Property 9: Thread-Safe Cache Access**
    - **Validates: Requirements 3.4**

  - [ ]* 4.9 Write property test for cache DataFrame format
    - **Property 10: Cache DataFrame Format**
    - **Validates: Requirements 3.6, 6.5**

  - [ ]* 4.10 Write property test for cache round-trip preservation
    - **Property 32: Cache Round-Trip Preservation**
    - **Validates: Requirements 25.6**

  - [ ]* 4.11 Write property test for current candle tick updates
    - **Property 31: Current Candle Tick Updates**
    - **Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.6**

- [ ] 5. Implement RateLimiter
  - [ ] 5.1 Create RateLimiter class with request tracking
    - Implement `acquire()` method with async delay enforcement
    - Track request counts per 10-minute window
    - Enforce minimum 1-second delay between requests
    - Limit to 60 requests per 10-minute window
    - _Requirements: 11.1, 11.2, 11.3_

  - [ ] 5.2 Implement pacing violation handling
    - Implement `record_pacing_violation()` method
    - Pause requests for 60 seconds on pacing error
    - Log rate limit events at WARNING level
    - _Requirements: 11.4, 11.6_

  - [ ] 5.3 Implement rate limit statistics
    - Implement `get_stats()` method
    - Track delays, violations, request counts
    - _Requirements: 16.1_

  - [ ]* 5.4 Write unit tests for rate limit handling
    - Test request delay enforcement
    - Test 10-minute window tracking
    - Test pacing violation pause
    - _Requirements: 25.4_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement HistoricalFetcher
  - [ ] 7.1 Create HistoricalFetcher class with IBKR API integration
    - Implement `fetch()` method for recent N candles
    - Implement `fetch_range()` method for date range queries
    - Implement `fetch_batch()` for concurrent multi-symbol fetching
    - Integrate with ContractResolver and RateLimiter
    - Support all timeframes (1m, 5m, 15m, 30m, 1h, 4h, 1d)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 9.4_

  - [ ] 7.2 Implement OHLCV data normalization
    - Convert IBKR bar data to standard DataFrame format
    - Ensure columns: [time, open, high, low, close, volume]
    - Convert timestamps to UTC timezone-aware
    - _Requirements: 2.7, 6.5, 6.6_

  - [ ] 7.3 Implement async request handling and error recovery
    - Execute requests asynchronously with asyncio
    - Queue failed requests for retry
    - Implement exponential backoff for retries
    - Handle timeouts (30 seconds)
    - Support up to 10 concurrent requests
    - _Requirements: 2.5, 2.6, 22.1, 22.3, 22.4, 22.5_

  - [ ] 7.4 Implement data quality validation
    - Validate high >= low for all candles
    - Validate high >= open and high >= close
    - Validate low <= open and low <= close
    - Reject negative or zero prices
    - Reject future timestamps
    - Handle duplicate timestamps (keep most recent)
    - Log validation failures
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

  - [ ]* 7.5 Write property test for historical fetch candle count bounds
    - **Property 4: Historical Fetch Candle Count Bounds**
    - **Validates: Requirements 2.2**

  - [ ]* 7.6 Write property test for rate limit queuing
    - **Property 5: Rate Limit Queuing**
    - **Validates: Requirements 2.5**

  - [ ]* 7.7 Write property test for historical data normalization
    - **Property 6: Historical Data Normalization**
    - **Validates: Requirements 2.7, 6.5, 6.6**

  - [ ]* 7.8 Write property test for OHLCV candle validity
    - **Property 25: OHLCV Candle Validity**
    - **Validates: Requirements 12.1, 12.2, 12.3, 12.5**

  - [ ]* 7.9 Write property test for invalid candle exclusion
    - **Property 26: Invalid Candle Exclusion**
    - **Validates: Requirements 12.4**

  - [ ]* 7.10 Write property test for future timestamp rejection
    - **Property 27: Future Timestamp Rejection**
    - **Validates: Requirements 12.6**

  - [ ]* 7.11 Write property test for duplicate timestamp deduplication
    - **Property 28: Duplicate Timestamp Deduplication**
    - **Validates: Requirements 12.7**

  - [ ]* 7.12 Write unit tests for historical fetcher
    - Test OHLCV normalization
    - Test timeframe mapping
    - Test error handling
    - _Requirements: 25.4_

- [ ] 8. Implement LiveStreamManager
  - [ ] 8.1 Create LiveStreamManager class with subscription management
    - Implement `subscribe()` method for live data subscriptions
    - Implement `unsubscribe()` method for cleanup
    - Track subscription metadata (reason, timestamp, priority)
    - Enforce 80 concurrent subscription limit
    - _Requirements: 5.1, 5.2, 5.4, 5.7_

  - [ ] 8.2 Implement subscription eviction logic
    - Implement age-based eviction when limit reached
    - Prioritize by reason: position > signal > manual > scanner
    - Never evict position-related subscriptions
    - Implement `_evict_oldest_non_position()` method
    - _Requirements: 5.5_

  - [ ] 8.3 Implement tick data processing
    - Implement `_on_tick()` callback for incoming ticks
    - Update OHLCVCache with live tick data
    - Call `update_current_candle()` on cache
    - _Requirements: 5.6_

  - [ ] 8.4 Implement subscription tracking and reporting
    - Implement `get_active_subscriptions()` method
    - Track subscription changes per hour
    - Enforce 100ms minimum delay between subscription changes
    - _Requirements: 11.5, 16.3_

  - [ ]* 8.5 Write property test for signal-triggered subscription
    - **Property 18: Signal-Triggered Subscription**
    - **Validates: Requirements 5.1**

  - [ ]* 8.6 Write property test for position-triggered subscription
    - **Property 19: Position-Triggered Subscription**
    - **Validates: Requirements 5.2**

  - [ ]* 8.7 Write property test for subscription cleanup
    - **Property 20: Subscription Cleanup**
    - **Validates: Requirements 5.3**

  - [ ]* 8.8 Write property test for subscription limit invariant
    - **Property 21: Subscription Limit Invariant**
    - **Validates: Requirements 5.4**

  - [ ]* 8.9 Write property test for eviction priority
    - **Property 22: Eviction Priority**
    - **Validates: Requirements 5.5**

  - [ ]* 8.10 Write property test for tick cache update
    - **Property 23: Tick Cache Update**
    - **Validates: Requirements 5.6**

  - [ ]* 8.11 Write unit tests for live stream manager
    - Test subscription lifecycle
    - Test limit enforcement
    - Test tick processing
    - _Requirements: 25.5_

- [ ] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Implement RotatingScanner
  - [ ] 10.1 Create RotatingScanner class with batch rotation logic
    - Implement batch creation with size constraints (20-50 symbols)
    - Implement `set_asset_universe()` method
    - Implement `_build_priority_batches()` method
    - Support configurable rotation delay (5-30 seconds)
    - _Requirements: 4.1, 4.4_

  - [ ] 10.2 Implement rotation loop
    - Implement `start()` method to begin rotation
    - Implement `stop()` method for graceful shutdown
    - Process batches sequentially without overlap
    - Skip symbols with active live streams
    - Coordinate with HistoricalFetcher for batch requests
    - Update cache after each batch
    - _Requirements: 4.2, 4.3, 4.6, 4.7_

  - [ ] 10.3 Implement priority-based scheduling
    - Implement `set_priority()` method
    - Prioritize symbols with positions (max priority)
    - Prioritize symbols with signals (high priority)
    - Recalculate priorities after each cycle
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6_

  - [ ] 10.4 Implement rotation monitoring and reporting
    - Implement `get_rotation_status()` method
    - Track current batch index, cycle count
    - Measure full rotation time
    - Log cycle completion
    - Ensure cycle time within configured limit (5-15 minutes)
    - _Requirements: 4.5, 16.2_

  - [ ]* 10.5 Write property test for batch size constraints
    - **Property 11: Batch Size Constraints**
    - **Validates: Requirements 4.1**

  - [ ]* 10.6 Write property test for sequential batch processing
    - **Property 12: Sequential Batch Processing**
    - **Validates: Requirements 4.2**

  - [ ]* 10.7 Write property test for complete batch coverage
    - **Property 13: Complete Batch Coverage**
    - **Validates: Requirements 4.3**

  - [ ]* 10.8 Write property test for batch delay compliance
    - **Property 14: Batch Delay Compliance**
    - **Validates: Requirements 4.4**

  - [ ]* 10.9 Write property test for rotation cycle time
    - **Property 15: Rotation Cycle Time**
    - **Validates: Requirements 4.5**

  - [ ]* 10.10 Write property test for live stream exclusion
    - **Property 16: Live Stream Exclusion**
    - **Validates: Requirements 4.6**

  - [ ]* 10.11 Write property test for rotation loop continuity
    - **Property 17: Rotation Loop Continuity**
    - **Validates: Requirements 4.7**

  - [ ]* 10.12 Write unit tests for rotating scanner
    - Test batch creation
    - Test rotation timing
    - Test priority scheduling
    - _Requirements: 25.7_

- [ ] 11. Implement IBKRMarketDataClient facade
  - [ ] 11.1 Create IBKRMarketDataClient class coordinating all components
    - Initialize all components (ConnectionManager, ContractResolver, etc.)
    - Implement `connect()` method to start all background tasks
    - Implement `disconnect()` method for graceful shutdown
    - Load configuration from environment
    - _Requirements: 13.1_

  - [ ] 11.2 Implement cache-first data retrieval interface
    - Implement `get_latest_candles()` method
    - Implement `get_historical_ohlcv()` method
    - Return cached data if available and not stale
    - Trigger background fetch for cache misses
    - _Requirements: 6.3, 6.4_

  - [ ] 11.3 Implement live subscription interface
    - Implement `subscribe_live()` method
    - Implement `unsubscribe_live()` method
    - Delegate to LiveStreamManager
    - _Requirements: 5.1, 5.2, 5.3_

  - [ ] 11.4 Implement health and metrics reporting
    - Implement `get_health_status()` method
    - Implement `get_metrics()` method
    - Aggregate status from all components
    - Track connection state, cache stats, rotation progress
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 24.1, 24.2, 24.3, 24.4, 24.5_

  - [ ]* 11.5 Write property test for cache-first data retrieval
    - **Property 24: Cache-First Data Retrieval**
    - **Validates: Requirements 6.3**

- [ ] 12. Implement error handling and logging
  - [ ] 12.1 Add comprehensive error handling for all components
    - Handle connection errors with reconnection logic
    - Handle rate limit violations with pause and retry
    - Handle contract resolution failures with skip and retry
    - Handle data validation failures with exclusion
    - Handle cache persistence errors gracefully
    - Handle subscription limit exceeded with eviction
    - Handle async timeouts with cancellation and retry
    - Handle memory pressure with cache pruning
    - _Requirements: 14.5_

  - [ ] 12.2 Implement structured logging throughout
    - Log INFO for: connection established, batch rotation, subscriptions
    - Log WARNING for: rate limits, reconnection, validation failures
    - Log ERROR for: connection failures, API errors, resolution failures
    - Include context: symbol, timeframe, timestamp in all logs
    - Log full stack traces for exceptions
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.6_

  - [ ]* 12.3 Write unit tests for error handling
    - Test connection error recovery
    - Test rate limit handling
    - Test validation failure handling
    - _Requirements: 25.4_

- [ ] 13. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Integrate with existing market_data module
  - [x] 14.1 Register IBKR as data source in market_data module
    - Add IBKR client initialization in market_data module
    - Implement source routing for IBKR requests
    - Support source parameter in get_historical_ohlcv()
    - Support source parameter in get_latest_candles()
    - _Requirements: 6.1, 6.2, 7.3, 7.4_

  - [x] 14.2 Ensure compatibility with SignalEngine
    - Verify IBKR data flows to SignalEngine.evaluate()
    - Test signal generation with IBKR-sourced data
    - _Requirements: 7.2, 7.5_

  - [ ]* 14.3 Write integration tests for signal engine
    - Test IBKR data feeding into signal generation
    - Test multi-source signal generation
    - _Requirements: 25.2_

- [x] 15. Integrate with IBKRAdapter execution layer
  - [x] 15.1 Coordinate connection sharing with IBKRAdapter
    - Share IB connection instance between data and execution
    - Use separate client IDs if needed
    - Coordinate connection lifecycle
    - Ensure order execution not blocked by data operations
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 15.2 Implement position-triggered subscriptions
    - Listen for trade open events from IBKRAdapter
    - Create live subscriptions for position symbols
    - Remove subscriptions when positions close
    - _Requirements: 5.2, 5.3_

- [x] 16. Integrate with MarketScanner
  - [x] 16.1 Connect IBKR data to MarketScanner
    - Provide IBKR data when configured as source
    - Support candidate ranking with IBKR data
    - _Requirements: 15.1, 15.2_

  - [x] 16.2 Implement priority-based scanning
    - Increase scan frequency for high-score candidates
    - Increase scan frequency for symbols with signals
    - _Requirements: 15.3, 15.4_

  - [x] 16.3 Integrate with symbol_mapper best_source()
    - Register IBKR-supported symbols in symbol_mapper
    - Implement ibkr_supported() function
    - Integrate with best_source() selection logic
    - _Requirements: 15.5, 21.1, 21.2, 21.3, 21.4, 21.5_

- [ ] 17. Implement startup initialization and shutdown
  - [ ] 17.1 Implement fast startup with progressive loading
    - Establish connection before loading cache
    - Load cache asynchronously in background
    - Start rotation immediately after connection
    - Log cache loading completion
    - Complete startup within 10 seconds (excluding cache)
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5_

  - [ ] 17.2 Implement graceful shutdown
    - Stop rotation loop
    - Unsubscribe all live streams
    - Persist cache to disk
    - Close IBKR connection
    - _Requirements: 18.1_

  - [ ] 17.3 Implement graceful degradation
    - Mark IBKR as unavailable on connection failure
    - Enable fallback to alternative sources
    - Log source changes at WARNING level
    - Periodically attempt reconnection in degraded mode
    - Log recovery when connection restored
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

- [ ] 18. Implement multi-timeframe support
  - [ ] 18.1 Add multi-timeframe caching
    - Maintain separate cache entries per symbol-timeframe
    - Support parallel or sequential timeframe scanning
    - _Requirements: 9.1, 9.2_

  - [ ] 18.2 Implement timeframe batching optimization
    - Batch requests by symbol across timeframes
    - Map timeframe strings to IBKR parameters
    - Handle unsupported timeframes gracefully
    - _Requirements: 9.3, 9.4, 9.5_

- [ ] 19. Add performance monitoring and optimization
  - [ ] 19.1 Implement comprehensive metrics tracking
    - Track request counts, failures, response times
    - Track cache hit rate, cache size
    - Track rotation metrics (batches/hour, symbols/minute)
    - Track subscription metrics
    - Track rate limit delays and violations
    - Reset hourly counters appropriately
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_

  - [ ] 19.2 Add periodic performance logging
    - Log performance summary every 60 minutes
    - Include all key metrics in summary
    - _Requirements: 16.6_

- [ ] 20. Final integration testing and validation
  - [ ]* 20.1 Write end-to-end integration test
    - Test historical fetch for Stock, Forex, Crypto
    - Test cache population and retrieval
    - Test graceful disconnect
    - _Requirements: 25.2_

  - [ ]* 20.2 Write live stream lifecycle integration test
    - Test subscription creation and tick processing
    - Test cache updates from live data
    - Test unsubscribe and cleanup
    - _Requirements: 25.2_

  - [ ]* 20.3 Write rotating scanner integration test
    - Test full rotation cycle with 100 symbols
    - Test batch processing and timing
    - Test cache population for all symbols
    - _Requirements: 25.2_

  - [ ]* 20.4 Write reconnection integration test
    - Test disconnect and reconnection
    - Test subscription restoration
    - Test rotation resume
    - _Requirements: 25.2_

  - [ ]* 20.5 Write rate limit integration test
    - Test rate limit detection and handling
    - Test request queuing and retry
    - _Requirements: 25.2_

  - [ ]* 20.6 Write multi-timeframe integration test
    - Test data fetching across multiple timeframes
    - Test separate cache entries
    - Test data consistency
    - _Requirements: 25.2_

- [ ] 21. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at key milestones
- Property tests validate universal correctness properties using Hypothesis library
- Unit tests validate specific examples and edge cases
- Integration tests verify component interactions with IBKR paper trading account
- The implementation uses Python with ib_insync, pandas, and asyncio libraries
- All components use async/await patterns for non-blocking operations
- Cache persistence uses parquet format for efficient storage
- Configuration is loaded from environment variables with sensible defaults
