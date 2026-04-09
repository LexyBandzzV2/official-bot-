# Requirements Document

## Introduction

This document specifies requirements for implementing a multi-broker routing and execution system for an algorithmic trading bot. The system will intelligently route trade orders to multiple brokers (Alpaca, Kraken, FXCM, IBKR) based on asset class, timeframe, and trading mode, while integrating local ML-based confidence scoring via XGBoost/LightGBM.

The system currently has a working IBKR integration with rotating data feed architecture. This feature extends the bot to support multiple brokers with intelligent routing rules optimized for fees and capabilities, per-broker risk limits, and local ML-powered trade filtering.

## Glossary

- **Broker_Router**: Component that routes trade orders to the appropriate broker adapter based on routing rules
- **Broker_Adapter**: Interface implementation for a specific broker's API (Alpaca, Kraken, FXCM, IBKR)
- **Trading_Mode**: Operational mode of the system, either "paper" (simulated) or "live" (real money)
- **Asset_Class**: Category of tradable instrument (stocks, ETFs, crypto, forex, commodities, indices)
- **Timeframe**: Candlestick interval for market data (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d)
- **Routing_Rule**: Logic that determines which broker handles a specific asset class and timeframe combination
- **Trade_Limit**: Maximum number of trades allowed per broker per hour
- **ML_Model**: Local machine learning model (XGBoost/LightGBM) providing trade signal confidence scoring without external API calls
- **Risk_Manager**: Component that enforces per-broker trade limits and risk parameters
- **Configuration_Loader**: Component that loads broker credentials and settings from environment variables
- **Market_Data_Router**: Component that routes market data requests to the appropriate broker's data feed

## Requirements

### Requirement 1: Broker Adapter Implementation

**User Story:** As a developer, I want standardized broker adapters for Alpaca, Kraken, and FXCM, so that the system can execute trades across multiple brokers with a consistent interface.

#### Acceptance Criteria

1. THE Alpaca_Adapter SHALL implement the same interface as IBKR_Adapter (place_order, close_order, update_trailing_stop, modify_position_sltp)
2. THE Kraken_Adapter SHALL implement the same interface as IBKR_Adapter
3. THE FXCM_Adapter SHALL implement the same interface as IBKR_Adapter
4. WHEN a broker adapter connects successfully, THE Broker_Adapter SHALL return True from the connect() method
5. WHEN a broker adapter fails to connect, THE Broker_Adapter SHALL log the error and return False from the connect() method
6. WHEN a place_order call succeeds, THE Broker_Adapter SHALL return a dictionary containing order_id, symbol, side, volume, price, stop_loss, take_profit, and status
7. WHEN a place_order call fails, THE Broker_Adapter SHALL log the error and return None

### Requirement 2: Intelligent Broker Routing for Live Trading

**User Story:** As a trader, I want orders routed to the optimal broker based on asset class and timeframe, so that I minimize trading fees and maximize execution quality.

#### Acceptance Criteria

1. WHEN Trading_Mode is "live" AND asset_class is "stocks" AND timeframe is in [3m, 5m, 15m, 30m], THE Broker_Router SHALL route the order to Alpaca_Adapter
2. WHEN Trading_Mode is "live" AND asset_class is "stocks" AND timeframe is in [15m, 30m, 1h, 2h, 4h], THE Broker_Router SHALL route the order to IBKR_Adapter
3. WHEN Trading_Mode is "live" AND asset_class is "etfs" AND timeframe is in [3m, 5m, 15m, 30m], THE Broker_Router SHALL route the order to Alpaca_Adapter
4. WHEN Trading_Mode is "live" AND asset_class is "etfs" AND timeframe is in [15m, 30m, 1h, 2h, 4h], THE Broker_Router SHALL route the order to IBKR_Adapter
5. WHEN Trading_Mode is "live" AND asset_class is "crypto" AND timeframe is in [3m, 5m, 15m, 30m], THE Broker_Router SHALL route the order to either Alpaca_Adapter or Kraken_Adapter
6. WHEN Trading_Mode is "live" AND asset_class is "forex" AND symbol represents a traditional currency pair, THE Broker_Router SHALL route the order to FXCM_Adapter
7. WHEN Trading_Mode is "live" AND asset_class is "forex" AND symbol represents a crypto exchange pair, THE Broker_Router SHALL route the order to Kraken_Adapter
8. WHEN Trading_Mode is "live" AND asset_class is "commodities", THE Broker_Router SHALL route the order to IBKR_Adapter
9. WHEN Trading_Mode is "live" AND asset_class is "indices", THE Broker_Router SHALL route the order to IBKR_Adapter
10. WHEN Trading_Mode is "live" AND asset_class is "crypto" AND timeframe is NOT in [3m, 5m, 15m, 30m], THE Broker_Router SHALL reject the order with reason "CRYPTO_TIMEFRAME_NOT_SUPPORTED"

### Requirement 3: Paper Trading Mode Routing

**User Story:** As a trader, I want unrestricted broker access in paper trading mode, so that I can test strategies across all asset classes and timeframes without limitations.

#### Acceptance Criteria

1. WHEN Trading_Mode is "paper", THE Broker_Router SHALL allow all brokers to trade all asset classes they support
2. WHEN Trading_Mode is "paper", THE Broker_Router SHALL allow all brokers to trade all timeframes
3. WHEN Trading_Mode is "paper", THE Broker_Router SHALL NOT enforce live mode routing restrictions

### Requirement 4: Per-Broker Trade Limits

**User Story:** As a risk manager, I want independent trade limits for each broker, so that I can control exposure across multiple accounts separately.

#### Acceptance Criteria

1. THE Risk_Manager SHALL track trade counts independently for each broker
2. WHEN a broker reaches its max_trades_per_hour limit, THE Risk_Manager SHALL reject new orders for that broker with reason "BROKER_HOURLY_LIMIT_EXCEEDED"
3. WHEN a broker has NOT reached its max_trades_per_hour limit, THE Risk_Manager SHALL allow new orders for that broker
4. WHEN the clock hour changes, THE Risk_Manager SHALL reset the trade count for all brokers to zero
5. THE Risk_Manager SHALL allow configurable max_trades_per_hour values per broker (default: 15)

### Requirement 5: Broker Configuration Loading

**User Story:** As a system administrator, I want all broker configurations loaded from environment variables, so that I can manage credentials and settings securely without code changes.

#### Acceptance Criteria

1. THE Configuration_Loader SHALL read Alpaca API credentials from ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables
2. THE Configuration_Loader SHALL read Kraken API credentials from KRAKEN_API_KEY and KRAKEN_SECRET environment variables
3. THE Configuration_Loader SHALL read FXCM API credentials from FXCM_API_KEY and FXCM_ACCESS_TOKEN environment variables
4. THE Configuration_Loader SHALL read per-broker account balances from {BROKER}_ACCOUNT_BALANCE environment variables
5. THE Configuration_Loader SHALL read per-broker trade limits from {BROKER}_MAX_TRADES_PER_HOUR environment variables
6. THE Configuration_Loader SHALL read broker-specific asset class restrictions from {BROKER}_ASSETS environment variables
7. THE Configuration_Loader SHALL read broker-specific timeframe restrictions from {BROKER}_TIMEFRAMES environment variables
8. THE Configuration_Loader SHALL read Trading_Mode from TRADING_MODE environment variable
9. WHEN an environment variable is missing, THE Configuration_Loader SHALL use a documented default value
10. THE Configuration_Loader SHALL read broker enabled/disabled status from {BROKER}_ENABLED environment variables

### Requirement 6: Local ML Model Integration (XGBoost/LightGBM)

**User Story:** As a trader, I want local ML-based confidence scoring for trade signals using XGBoost and LightGBM, so that I can filter low-quality signals without external API dependencies or costs.

#### Acceptance Criteria

1. THE ML_Model_Client SHALL support both XGBoost and LightGBM models for trade signal confidence scoring
2. THE ML_Model_Client SHALL load trained models from the models/ directory (xgboost_model.json, lightgbm_model.txt)
3. THE ML_Model_Client SHALL extract features from trade signals including: open, high, low, close, volume, ATR, vortex indicators, stochastic values, alligator lines, candle metrics, volatility, hour_of_day, day_of_week
4. WHEN scoring a signal, THE ML_Model_Client SHALL return a confidence score between 0.0 and 1.0 representing the probability the trade will be profitable
5. WHEN no trained model exists, THE ML_Model_Client SHALL return None and log a warning
6. THE ML_Model_Client SHALL provide a train_model() method that trains on historical signal data from Supabase (buy_signals, sell_signals, trades tables)
7. WHEN training, THE ML_Model_Client SHALL use 80/20 train/test split with stratification
8. THE ML_Model_Client SHALL save trained models to models/xgboost_model.json and models/lightgbm_model.txt
9. THE ML_Model_Client SHALL log training metrics: accuracy, precision, recall, F1 score, feature importance
10. THE ML_Model_Client SHALL support model retraining via a retrain_ml_models() function callable from the bot
11. THE ML_Model_Client SHALL use the existing ml_features table in Supabase to store training data
12. THE System SHALL install required libraries: xgboost, lightgbm, scikit-learn, pandas, numpy, pyarrow

#### Correctness Properties

**Property ML_CONFIDENCE_RANGE**: For all signals s, when ML_Model_Client.score_signal(s) returns a value v, then 0.0 ≤ v ≤ 1.0

**Property ML_FEATURE_CONSISTENCY**: For all signals s, the feature vector extracted by ML_Model_Client SHALL contain exactly the same features in the same order as used during training

**Property ML_MODEL_PERSISTENCE**: When a model is saved via save_model(), loading that model via load_model() SHALL produce identical predictions for the same input

### Requirement 7: Market Data Source Routing

**User Story:** As a system operator, I want market data requests routed to the appropriate broker, so that I receive accurate and timely price data for each asset class.

#### Acceptance Criteria

1. WHEN requesting market data for an asset, THE Market_Data_Router SHALL determine the appropriate data source based on asset class
2. WHEN a broker is configured as the data source for an asset, THE Market_Data_Router SHALL use that broker's market data feed
3. WHEN multiple brokers support an asset, THE Market_Data_Router SHALL select the broker with the most reliable data feed
4. WHEN a broker's data feed fails, THE Market_Data_Router SHALL log the error and attempt a fallback data source
5. THE Market_Data_Router SHALL support IBKR as a data source option via the source parameter

### Requirement 8: Broker Connection Management

**User Story:** As a system operator, I want automatic broker connection handling, so that the system gracefully handles connection failures and reconnections.

#### Acceptance Criteria

1. WHEN Broker_Router initializes, THE Broker_Router SHALL attempt to connect all enabled brokers
2. WHEN at least one broker connects successfully, THE Broker_Router SHALL return True from connect() method
3. WHEN all brokers fail to connect, THE Broker_Router SHALL return False from connect() method
4. WHEN a broker is disabled via {BROKER}_ENABLED=false, THE Broker_Router SHALL NOT attempt to connect that broker
5. WHEN Broker_Router disconnects, THE Broker_Router SHALL disconnect all connected broker adapters
6. THE Broker_Router SHALL log connection status for each broker

### Requirement 9: Order Execution with Stop Loss and Take Profit

**User Story:** As a trader, I want all orders placed with automatic stop loss and take profit orders, so that my risk is managed automatically per the 2% stop loss and trailing take profit rules.

#### Acceptance Criteria

1. WHEN placing an order, THE Broker_Adapter SHALL place a market order for the main position
2. WHEN the main order fills, THE Broker_Adapter SHALL place a stop loss order at the specified stop_loss price
3. WHEN the main order fills AND take_profit is provided, THE Broker_Adapter SHALL place a take profit limit order at the specified take_profit price
4. WHEN placing orders, THE Broker_Adapter SHALL store the trade_id, contract, main order, stop order, and take profit order for later updates
5. WHEN an order placement fails, THE Broker_Adapter SHALL log the error with the symbol and reason

### Requirement 10: Trailing Stop and Take Profit Updates

**User Story:** As a trader, I want automatic trailing stop and take profit updates sent to brokers, so that my protective orders track market movement in real-time.

#### Acceptance Criteria

1. WHEN a trailing stop value changes, THE Broker_Adapter SHALL cancel the old stop order and place a new stop order at the updated price
2. WHEN a take profit value changes, THE Broker_Adapter SHALL cancel the old take profit order and place a new limit order at the updated price
3. WHEN updating stop loss or take profit, THE Broker_Adapter SHALL use the stored trade_id to identify the correct position
4. WHEN a stop loss or take profit update fails, THE Broker_Adapter SHALL log the error and return False
5. WHEN a stop loss or take profit update succeeds, THE Broker_Adapter SHALL return True

### Requirement 11: Position Closing

**User Story:** As a trader, I want positions closed automatically when exit conditions are met, so that profits are locked in and losses are limited.

#### Acceptance Criteria

1. WHEN closing a position, THE Broker_Adapter SHALL cancel any active stop loss orders for that position
2. WHEN closing a position, THE Broker_Adapter SHALL cancel any active take profit orders for that position
3. WHEN closing a position, THE Broker_Adapter SHALL place a reverse market order to close the position
4. WHEN a position close succeeds, THE Broker_Adapter SHALL remove the trade from active trades and return True
5. WHEN a position close fails, THE Broker_Adapter SHALL log the error and return False

### Requirement 12: Broker-Specific Account Balance Tracking

**User Story:** As a trader, I want separate account balances tracked per broker, so that position sizing is calculated correctly for each broker's available capital.

#### Acceptance Criteria

1. THE Risk_Manager SHALL maintain separate account balance values for each broker
2. WHEN calculating position size, THE Risk_Manager SHALL use the account balance of the target broker
3. WHEN a trade closes with profit or loss, THE Risk_Manager SHALL update the account balance of the broker that executed the trade
4. THE Configuration_Loader SHALL load initial account balances from {BROKER}_ACCOUNT_BALANCE environment variables

### Requirement 13: Routing Rule Configuration Parser

**User Story:** As a developer, I want routing rules loaded from configuration, so that I can modify broker routing logic without code changes.

#### Acceptance Criteria

1. THE Configuration_Loader SHALL parse asset class lists from {BROKER}_ASSETS environment variables
2. THE Configuration_Loader SHALL parse timeframe lists from {BROKER}_TIMEFRAMES environment variables
3. WHEN {BROKER}_TIMEFRAMES is set to "all", THE Configuration_Loader SHALL allow all timeframes for that broker
4. WHEN parsing comma-separated lists, THE Configuration_Loader SHALL trim whitespace from each item
5. THE Configuration_Loader SHALL validate that asset class values are in the set [stocks, etfs, crypto, forex, forex_crypto, commodities, indices]
6. WHEN an invalid asset class is specified, THE Configuration_Loader SHALL log a warning and skip that asset class

### Requirement 14: Broker Selection Logic

**User Story:** As a system operator, I want the router to select the best broker when multiple brokers support an asset, so that trades are executed with optimal fees and reliability.

#### Acceptance Criteria

1. WHEN multiple brokers support an asset class and timeframe combination, THE Broker_Router SHALL select the broker with the lowest fees
2. WHEN a selected broker is unavailable or disconnected, THE Broker_Router SHALL attempt to route to an alternative broker that supports the asset
3. WHEN no broker supports the requested asset class and timeframe combination, THE Broker_Router SHALL reject the order with reason "NO_BROKER_AVAILABLE"
4. WHEN a broker is disabled, THE Broker_Router SHALL NOT route orders to that broker

### Requirement 15: ML Model Health Monitoring

**User Story:** As a system operator, I want automatic health checks for ML models, so that the system gracefully handles model loading failures or prediction errors.

#### Acceptance Criteria

1. THE ML_Model_Client SHALL check if trained models exist before scoring signals
2. WHEN trained models are missing, THE ML_Model_Client SHALL log a warning and return None from score_signal()
3. WHEN a prediction fails, THE ML_Model_Client SHALL log the error with signal details and return None
4. THE ML_Model_Client SHALL track prediction latency and log warnings if predictions take longer than 100ms
5. THE System SHALL create an ml_model_health table in Supabase with columns: id, timestamp, model_type (xgboost/lightgbm), is_loaded, avg_prediction_time_ms, predictions_count, errors_count, last_error_message
6. THE ML_Model_Client SHALL update ml_model_health table every 5 minutes with health metrics

### Requirement 16: Broker-Specific Error Handling

**User Story:** As a trader, I want detailed error logging for broker failures, so that I can diagnose and resolve connection or execution issues quickly.

#### Acceptance Criteria

1. WHEN a broker API call fails, THE Broker_Adapter SHALL log the broker name, operation type, symbol, and error message
2. WHEN a broker connection fails, THE Broker_Adapter SHALL log the broker name, connection parameters, and error message
3. WHEN a broker rejects an order, THE Broker_Adapter SHALL log the rejection reason provided by the broker
4. THE Broker_Adapter SHALL include the trade_id in all error logs for order operations
5. WHEN a broker adapter encounters an exception, THE Broker_Adapter SHALL NOT crash the application

### Requirement 17: Multi-Broker Position Tracking

**User Story:** As a trader, I want positions tracked per broker, so that I can monitor exposure across all my brokerage accounts.

#### Acceptance Criteria

1. THE Broker_Router SHALL maintain a mapping of trade_id to broker name for all open positions
2. WHEN querying open positions, THE Broker_Router SHALL aggregate positions from all connected brokers
3. WHEN updating a position, THE Broker_Router SHALL route the update to the broker that holds the position
4. WHEN closing a position, THE Broker_Router SHALL route the close request to the broker that holds the position
5. THE Broker_Router SHALL handle cases where a trade_id is not found in any broker's active trades

### Requirement 18: Configuration Validation

**User Story:** As a system administrator, I want configuration validation on startup, so that I catch misconfiguration errors before trading begins.

#### Acceptance Criteria

1. WHEN the system starts, THE Configuration_Loader SHALL validate that at least one broker is enabled
2. WHEN a broker is enabled, THE Configuration_Loader SHALL validate that required API credentials are present
3. WHEN Trading_Mode is "live", THE Configuration_Loader SHALL validate that routing rules are defined for all enabled brokers
4. WHEN configuration validation fails, THE Configuration_Loader SHALL log all validation errors
5. WHEN critical configuration is missing, THE System SHALL refuse to start and display an error message

### Requirement 19: Broker Capability Discovery

**User Story:** As a developer, I want brokers to declare their supported asset classes and timeframes, so that the router can make decisions based on actual broker capabilities.

#### Acceptance Criteria

1. THE Broker_Adapter SHALL provide a get_supported_assets() method that returns a list of supported asset classes
2. THE Broker_Adapter SHALL provide a get_supported_timeframes() method that returns a list of supported timeframes
3. WHEN initializing, THE Broker_Router SHALL query each broker's capabilities
4. WHEN routing an order, THE Broker_Router SHALL verify the target broker supports the asset class and timeframe
5. WHEN a broker does not support the requested asset or timeframe, THE Broker_Router SHALL NOT route the order to that broker

### Requirement 20: ML Model Feature Engineering

**User Story:** As a trader, I want comprehensive feature extraction from signals, so that the ML models have rich data to learn from.

#### Acceptance Criteria

1. WHEN extracting features from a signal, THE ML_Model_Client SHALL calculate: candle_range (high - low), candle_body (abs(close - open)), volatility_10 (10-period rolling std of returns), volatility_20 (20-period rolling std of returns)
2. THE ML_Model_Client SHALL extract time-based features: hour_of_day, day_of_week from the signal timestamp
3. THE ML_Model_Client SHALL include all indicator values: alligator_jaw, alligator_teeth, alligator_lips, vortex_vi_plus, vortex_vi_minus, stochastic_k, stochastic_d, ATR
4. THE ML_Model_Client SHALL normalize feature values to prevent scale bias (using StandardScaler or MinMaxScaler)
5. THE ML_Model_Client SHALL handle missing features by filling with median values from training data
6. THE ML_Model_Client SHALL save feature scaler parameters to models/feature_scaler.pkl for consistent inference
7. WHEN training, THE ML_Model_Client SHALL log feature importance scores to identify which indicators contribute most to predictions

### Requirement 21: Broker Adapter Lazy Loading

**User Story:** As a developer, I want broker adapters loaded only when needed, so that the system starts quickly and doesn't require all broker dependencies to be installed.

#### Acceptance Criteria

1. WHEN Broker_Router initializes, THE Broker_Router SHALL NOT import broker adapter modules
2. WHEN a broker is first used, THE Broker_Router SHALL dynamically import the broker adapter module
3. WHEN a broker adapter import fails, THE Broker_Router SHALL log the error and mark that broker as unavailable
4. WHEN a broker is marked unavailable, THE Broker_Router SHALL NOT attempt to route orders to that broker
5. THE Broker_Router SHALL cache imported broker adapter instances for reuse

### Requirement 22: Market Data Source Selection

**User Story:** As a trader, I want market data fetched from the same broker that will execute trades, so that I minimize price discrepancies between data and execution.

#### Acceptance Criteria

1. WHEN fetching market data for a symbol, THE Market_Data_Router SHALL determine which broker will execute trades for that symbol
2. WHEN the execution broker provides market data, THE Market_Data_Router SHALL use that broker's data feed
3. WHEN the execution broker does NOT provide market data, THE Market_Data_Router SHALL use the existing data source fallback logic
4. THE Market_Data_Router SHALL support forcing a specific data source via the source parameter
5. WHEN source parameter is "ibkr", THE Market_Data_Router SHALL use IBKR_Market_Data_Client

### Requirement 23: Broker Adapter Connection Coordination

**User Story:** As a system operator, I want execution adapters to use separate client IDs from data feed clients, so that multiple connections to the same broker don't conflict.

#### Acceptance Criteria

1. WHEN connecting to IBKR for execution, THE IBKR_Adapter SHALL use client_id + 1 to avoid conflicts with the market data client
2. WHEN connecting to a broker, THE Broker_Adapter SHALL log the client ID or connection identifier being used
3. THE Broker_Adapter SHALL support configurable client ID offsets via constructor parameters
4. WHEN multiple adapters connect to the same broker, THE System SHALL ensure each uses a unique client ID

### Requirement 24: Broker Fee Optimization

**User Story:** As a trader, I want the router to prefer lower-fee brokers when multiple options exist, so that I maximize net profitability.

#### Acceptance Criteria

1. WHEN Trading_Mode is "live" AND both FXCM and IBKR support a forex pair, THE Broker_Router SHALL prefer FXCM due to lower fees
2. WHEN Trading_Mode is "live" AND both Alpaca and IBKR support a stock on overlapping timeframes, THE Broker_Router SHALL prefer the broker with lower fees for that timeframe
3. THE Broker_Router SHALL document fee comparison logic in code comments
4. THE Broker_Router SHALL allow manual broker selection override via configuration

### Requirement 25: Graceful Degradation

**User Story:** As a trader, I want the system to continue operating when some brokers are unavailable, so that I don't lose all trading capability due to a single broker outage.

#### Acceptance Criteria

1. WHEN a broker fails to connect, THE Broker_Router SHALL mark that broker as unavailable and continue initialization
2. WHEN routing an order to an unavailable broker, THE Broker_Router SHALL attempt to route to an alternative broker
3. WHEN no alternative broker is available, THE Broker_Router SHALL reject the order with reason "ALL_BROKERS_UNAVAILABLE"
4. WHEN a broker becomes available after being unavailable, THE Broker_Router SHALL allow routing to that broker
5. THE Broker_Router SHALL log broker availability status changes

### Requirement 26: Trade Execution Logging

**User Story:** As a trader, I want detailed logs of all order placements and updates, so that I can audit execution quality and troubleshoot issues.

#### Acceptance Criteria

1. WHEN placing an order, THE Broker_Adapter SHALL log the broker name, signal type, symbol, volume, entry price, stop loss, and take profit
2. WHEN an order fills, THE Broker_Adapter SHALL log the fill price and order ID
3. WHEN updating stop loss or take profit, THE Broker_Adapter SHALL log the old value, new value, and trade_id
4. WHEN closing a position, THE Broker_Adapter SHALL log the close price, PnL, and close reason
5. THE Broker_Adapter SHALL include timestamps in all log entries

### Requirement 27: Broker Routing Rule Validation

**User Story:** As a developer, I want routing rules validated against broker capabilities, so that configuration errors are caught early.

#### Acceptance Criteria

1. WHEN loading routing rules, THE Broker_Router SHALL verify that each configured asset class is supported by at least one broker
2. WHEN loading routing rules, THE Broker_Router SHALL verify that each configured timeframe is supported by at least one broker
3. WHEN a routing rule references an unsupported asset class, THE Broker_Router SHALL log a warning
4. WHEN a routing rule references an unsupported timeframe, THE Broker_Router SHALL log a warning
5. THE Broker_Router SHALL validate routing rules during initialization before accepting any orders

### Requirement 28: Broker Adapter Interface Consistency

**User Story:** As a developer, I want all broker adapters to implement the same interface, so that the router can treat all brokers uniformly.

#### Acceptance Criteria

1. THE Broker_Adapter interface SHALL define methods: connect(), disconnect(), place_order(), close_order(), update_trailing_stop(), modify_position_sltp()
2. THE place_order() method SHALL accept parameters: signal_type, symbol, volume, expected_entry, stop_loss, trade_id, and optional take_profit
3. THE close_order() method SHALL accept parameter: trade_id
4. THE update_trailing_stop() method SHALL accept parameters: trade_id, new_sl
5. THE modify_position_sltp() method SHALL accept parameters: trade_id, new_sl, optional new_tp
6. ALL Broker_Adapter implementations SHALL follow the same return type conventions (dict for success, None for failure, bool for status operations)

### Requirement 29: Per-Broker Risk Parameters

**User Story:** As a risk manager, I want different risk parameters per broker, so that I can adjust position sizing and limits based on each broker's account size and characteristics.

#### Acceptance Criteria

1. THE Risk_Manager SHALL support per-broker account_balance configuration
2. THE Risk_Manager SHALL support per-broker max_trades_per_hour configuration
3. WHEN calculating position size, THE Risk_Manager SHALL use the account balance of the broker that will execute the trade
4. WHEN checking trade limits, THE Risk_Manager SHALL use the max_trades_per_hour of the broker that will execute the trade
5. THE Risk_Manager SHALL maintain separate daily PnL tracking per broker

### Requirement 30: Broker Status Monitoring

**User Story:** As a system operator, I want to query broker connection status and statistics, so that I can monitor system health.

#### Acceptance Criteria

1. THE Broker_Router SHALL provide a get_broker_status() method that returns connection status for all brokers
2. THE Broker_Router SHALL provide a get_broker_balances() method that returns account balances for all connected brokers
3. THE Broker_Router SHALL provide a get_broker_trade_counts() method that returns current hourly trade counts per broker
4. WHEN a broker is disconnected, THE get_broker_status() method SHALL indicate that broker as "disconnected"
5. WHEN a broker is connected, THE get_broker_status() method SHALL indicate that broker as "connected"

### Requirement 31: Supabase Data Persistence for Multi-Broker Operations

**User Story:** As a trader, I want all broker execution data, routing decisions, and performance metrics persisted to Supabase, so that I can analyze multi-broker performance and maintain audit trails.

#### Acceptance Criteria

1. THE System SHALL extend the existing trades table with a broker_name column to track which broker executed each trade
2. THE System SHALL create a broker_executions table in Supabase with columns: id, trade_id, broker_name, order_id, order_type (market/stop/limit), symbol, side, volume, price, timestamp, status, error_message
3. THE System SHALL create a broker_routing_decisions table in Supabase with columns: id, timestamp, symbol, asset_class, timeframe, trading_mode, selected_broker, alternative_brokers, routing_reason, rejection_reason
4. THE System SHALL create a broker_metrics table in Supabase with columns: id, broker_name, timestamp, connection_status, account_balance, trades_this_hour, total_trades_today, total_pnl_today, avg_execution_time_ms, error_count
5. WHEN a trade is opened via any broker, THE Broker_Adapter SHALL call save_trade_open() with broker_name included in the TradeRecord
6. WHEN an order is placed, THE Broker_Adapter SHALL insert a record into broker_executions table with order details
7. WHEN an order status changes (filled, cancelled, rejected), THE Broker_Adapter SHALL update the broker_executions table
8. WHEN the Broker_Router makes a routing decision, THE Broker_Router SHALL insert a record into broker_routing_decisions table
9. WHEN a routing decision is rejected (no broker available, limits exceeded), THE Broker_Router SHALL insert a record into broker_routing_decisions with rejection_reason
10. THE Broker_Router SHALL update broker_metrics table every 60 seconds with current status for all brokers
11. THE System SHALL use the existing Supabase client from src/data/db.py for all multi-broker data persistence
12. WHEN Supabase is unavailable, THE System SHALL fall back to SQLite and log a warning (consistent with existing behavior)
13. THE System SHALL create corresponding SQLite tables for broker_executions, broker_routing_decisions, and broker_metrics for local fallback

### Requirement 32: ML Model Confidence Score Persistence

**User Story:** As a trader, I want ML confidence scores saved with each signal, so that I can analyze the correlation between ML predictions and trade outcomes.

#### Acceptance Criteria

1. WHEN an ML model scores a signal, THE ML_Model_Client SHALL return a dictionary containing: confidence_score (float 0.0-1.0), model_type (xgboost/lightgbm), prediction_time_ms, timestamp
2. WHEN saving a signal to the database, THE System SHALL store the ML confidence score in the ml_confidence column (already exists in buy_signals and sell_signals tables)
3. WHEN ML models are unavailable, THE System SHALL set ml_confidence to NULL in the database
4. THE System SHALL add an ml_model_metadata column (JSON type) to buy_signals and sell_signals tables to store: model_type, prediction_time_ms, feature_count, model_version
5. WHEN a trade is opened, THE System SHALL copy the ML confidence score from the signal to the trades table ml_confidence column
6. THE System SHALL use the existing ml_features table to store feature vectors and outcomes for continuous model retraining
7. WHEN a trade closes, THE System SHALL call save_ml_features() to store the feature vector and outcome (pnl_pct) for future training
8. THE ML_Model_Client SHALL support automatic retraining when ml_features table reaches 1000+ closed trades

### Requirement 33: Market Data Source Tracking

**User Story:** As a system operator, I want to track which broker provided market data for each signal, so that I can identify data quality issues and optimize data source selection.

#### Acceptance Criteria

1. THE System SHALL add a data_source column to buy_signals and sell_signals tables to track which broker/feed provided the market data (values: ibkr, alpaca, kraken, fxcm, coinbase, finnhub)
2. WHEN fetching market data, THE Market_Data_Router SHALL return the data source name along with the OHLCV data
3. WHEN saving a signal, THE System SHALL populate the data_source column with the broker/feed that provided the data
4. THE System SHALL create a market_data_quality table in Supabase with columns: id, data_source, symbol, timeframe, timestamp, candle_count, missing_candles, data_gaps, latency_ms, error_count
5. WHEN market data is fetched, THE Market_Data_Router SHALL log quality metrics to market_data_quality table
6. WHEN a data source fails, THE Market_Data_Router SHALL increment error_count in market_data_quality table
7. THE System SHALL aggregate market_data_quality metrics hourly to identify unreliable data sources

### Requirement 34: ML Model Training Pipeline

**User Story:** As a trader, I want an automated ML training pipeline that learns from my closed trades, so that the models continuously improve as more data becomes available.

#### Acceptance Criteria

1. THE System SHALL create a src/ml/ directory containing: model.py (ML model wrapper), train.py (training script), features.py (feature extraction)
2. THE ML_Training_Pipeline SHALL load closed trades from Supabase trades table where status='CLOSED'
3. THE ML_Training_Pipeline SHALL extract features from each closed trade using the same feature extraction logic as inference
4. THE ML_Training_Pipeline SHALL label trades as: 1 (win) if pnl_pct > 0, 0 (loss) if pnl_pct ≤ 0
5. THE ML_Training_Pipeline SHALL require minimum 200 closed trades before training (100 wins, 100 losses minimum)
6. WHEN training, THE ML_Training_Pipeline SHALL split data 80/20 train/test with stratification
7. THE ML_Training_Pipeline SHALL train both XGBoost and LightGBM models and compare F1 scores
8. THE ML_Training_Pipeline SHALL save the better-performing model as the primary model
9. THE ML_Training_Pipeline SHALL log training results to Supabase ml_training_runs table with columns: id, timestamp, model_type, train_samples, test_samples, accuracy, precision, recall, f1_score, feature_importance_json
10. THE System SHALL provide a CLI command: python -m src.ml.train to manually trigger retraining
11. THE ML_Training_Pipeline SHALL automatically retrain models every 7 days if 50+ new closed trades exist
12. THE ML_Training_Pipeline SHALL use Optuna for hyperparameter tuning after 500+ closed trades are available

### Requirement 35: ML Model Ensemble and Confidence Thresholding

**User Story:** As a trader, I want configurable confidence thresholds and model ensembling, so that I can optimize the trade-off between signal quantity and quality.

#### Acceptance Criteria

1. THE ML_Model_Client SHALL support ensemble predictions by averaging XGBoost and LightGBM confidence scores
2. THE System SHALL read ML_CONFIDENCE_THRESHOLD from environment variable (default: 0.65)
3. WHEN a signal's ML confidence is below ML_CONFIDENCE_THRESHOLD, THE System SHALL mark the signal as rejected with rejection_reason="ML_CONFIDENCE_TOO_LOW"
4. THE System SHALL log confidence threshold analysis to ml_threshold_analysis table with columns: id, timestamp, threshold, signals_above_threshold, win_rate_above_threshold, signals_below_threshold, win_rate_below_threshold
5. THE ML_Model_Client SHALL update ml_threshold_analysis table daily to help traders optimize their threshold
6. THE System SHALL support A/B testing by allowing different thresholds per broker via {BROKER}_ML_THRESHOLD environment variables
7. WHEN both XGBoost and LightGBM models are available, THE ML_Model_Client SHALL use ensemble averaging; otherwise use the single available model

