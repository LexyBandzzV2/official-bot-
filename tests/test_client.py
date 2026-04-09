"""
Tests for IBKRMarketDataClient facade.
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from src.data.ibkr.client import IBKRMarketDataClient
from src.data.ibkr.config import IBKRConfig


@pytest.fixture
def mock_config():
    """Create a test configuration."""
    return IBKRConfig(
        host="127.0.0.1",
        port=7497,
        client_id=2,
        batch_size=30,
        rotation_delay_seconds=10,
        rotation_cycle_minutes=10,
        max_live_subscriptions=80,
        historical_candle_count=200,
        cache_retention_hours=24,
        rate_limit_requests_per_10min=60,
        min_request_delay_seconds=1.0,
        reconnect_max_attempts=5,
        reconnect_backoff_base=2.0
    )


@pytest.fixture
def client(mock_config):
    """Create a client instance with mock config."""
    return IBKRMarketDataClient(config=mock_config)


def test_client_initialization(client, mock_config):
    """Test client initializes with correct configuration."""
    assert client.config == mock_config
    assert client.connection_manager is None
    assert client.contract_resolver is None
    assert client.cache is None
    assert client.rate_limiter is None
    assert client.historical_fetcher is None
    assert client.live_stream_manager is None
    assert client.rotating_scanner is None
    assert client._connected is False


@pytest.mark.asyncio
async def test_connect_initializes_components(client):
    """Test connect() initializes all components."""
    # Mock ConnectionManager
    with patch('src.data.ibkr.client.ConnectionManager') as MockConnectionManager, \
         patch('src.data.ibkr.client.ContractResolver') as MockContractResolver, \
         patch('src.data.ibkr.client.OHLCVCache') as MockCache, \
         patch('src.data.ibkr.client.RateLimiter') as MockRateLimiter, \
         patch('src.data.ibkr.client.HistoricalFetcher') as MockHistoricalFetcher, \
         patch('src.data.ibkr.client.LiveStreamManager') as MockLiveStreamManager, \
         patch('src.data.ibkr.client.RotatingScanner') as MockRotatingScanner:
        
        # Setup mocks
        mock_conn_mgr = MockConnectionManager.return_value
        mock_conn_mgr.connect = AsyncMock(return_value=True)
        mock_conn_mgr.is_connected = MagicMock(return_value=True)
        mock_conn_mgr.get_ib_instance = MagicMock(return_value=MagicMock())
        
        mock_cache = MockCache.return_value
        mock_cache.load = AsyncMock()
        
        # Connect
        success = await client.connect()
        
        # Verify
        assert success is True
        assert client._connected is True
        assert client.connection_manager is not None
        assert client.contract_resolver is not None
        assert client.cache is not None
        assert client.rate_limiter is not None
        assert client.historical_fetcher is not None
        assert client.live_stream_manager is not None
        assert client.rotating_scanner is not None


@pytest.mark.asyncio
async def test_connect_failure(client):
    """Test connect() handles connection failure."""
    with patch('src.data.ibkr.client.ConnectionManager') as MockConnectionManager:
        mock_conn_mgr = MockConnectionManager.return_value
        mock_conn_mgr.connect = AsyncMock(return_value=False)
        
        success = await client.connect()
        
        assert success is False
        assert client._connected is False


@pytest.mark.asyncio
async def test_disconnect_stops_components(client):
    """Test disconnect() stops all components gracefully."""
    # Setup connected client
    client._connected = True
    client.rotating_scanner = MagicMock()
    client.rotating_scanner.stop = AsyncMock()
    
    client.live_stream_manager = MagicMock()
    client.live_stream_manager.get_active_subscriptions = MagicMock(return_value=[
        {'symbol': 'AAPL'},
        {'symbol': 'MSFT'}
    ])
    client.live_stream_manager.unsubscribe = AsyncMock()
    
    client.cache = MagicMock()
    client.cache.persist = AsyncMock()
    
    client.connection_manager = MagicMock()
    client.connection_manager.disconnect = AsyncMock()
    
    # Disconnect
    await client.disconnect()
    
    # Verify
    assert client._connected is False
    client.rotating_scanner.stop.assert_called_once()
    assert client.live_stream_manager.unsubscribe.call_count == 2
    client.cache.persist.assert_called_once()
    client.connection_manager.disconnect.assert_called_once()


def test_get_latest_candles_cache_hit(client):
    """Test get_latest_candles returns cached data."""
    client._connected = True
    client.cache = MagicMock()
    
    # Mock cache hit
    expected_df = pd.DataFrame({
        'time': [datetime.now(timezone.utc)],
        'open': [100.0],
        'high': [101.0],
        'low': [99.0],
        'close': [100.5],
        'volume': [1000.0]
    })
    client.cache.get = MagicMock(return_value=expected_df)
    
    # Get candles
    result = client.get_latest_candles('AAPL', '5m', 200)
    
    # Verify
    assert not result.empty
    assert len(result) == 1
    client.cache.get.assert_called_once_with('AAPL', '5m', 200)


def test_get_latest_candles_cache_miss(client):
    """Test get_latest_candles handles cache miss."""
    client._connected = True
    client.cache = MagicMock()
    client.historical_fetcher = MagicMock()
    
    # Mock cache miss
    empty_df = pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    client.cache.get = MagicMock(return_value=empty_df)
    
    # Get candles
    result = client.get_latest_candles('AAPL', '5m', 200)
    
    # Verify
    assert result.empty
    client.cache.get.assert_called_once_with('AAPL', '5m', 200)


def test_get_latest_candles_not_connected(client):
    """Test get_latest_candles returns empty when not connected."""
    client._connected = False
    
    result = client.get_latest_candles('AAPL', '5m', 200)
    
    assert result.empty


@pytest.mark.asyncio
async def test_get_historical_ohlcv_cache_hit(client):
    """Test get_historical_ohlcv returns cached data when available."""
    client._connected = True
    client.cache = MagicMock()
    client.historical_fetcher = MagicMock()
    
    # Mock cache hit with data in range
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    
    cached_df = pd.DataFrame({
        'time': [
            datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 18, 0, tzinfo=timezone.utc)
        ],
        'open': [100.0, 101.0],
        'high': [101.0, 102.0],
        'low': [99.0, 100.0],
        'close': [100.5, 101.5],
        'volume': [1000.0, 1100.0]
    })
    client.cache.get = MagicMock(return_value=cached_df)
    
    # Get historical data
    result = await client.get_historical_ohlcv('AAPL', '1h', start, end)
    
    # Verify
    assert not result.empty
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_historical_ohlcv_cache_miss(client):
    """Test get_historical_ohlcv fetches from IBKR on cache miss."""
    client._connected = True
    client.cache = MagicMock()
    client.historical_fetcher = MagicMock()
    
    # Mock cache miss
    empty_df = pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    client.cache.get = MagicMock(return_value=empty_df)
    
    # Mock historical fetch
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    
    fetched_df = pd.DataFrame({
        'time': [datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)],
        'open': [100.0],
        'high': [101.0],
        'low': [99.0],
        'close': [100.5],
        'volume': [1000.0]
    })
    client.historical_fetcher.fetch_range = AsyncMock(return_value=fetched_df)
    
    # Get historical data
    result = await client.get_historical_ohlcv('AAPL', '1h', start, end)
    
    # Verify
    assert not result.empty
    client.historical_fetcher.fetch_range.assert_called_once()
    client.cache.update.assert_called_once()


@pytest.mark.asyncio
async def test_subscribe_live(client):
    """Test subscribe_live delegates to LiveStreamManager."""
    client._connected = True
    client.live_stream_manager = MagicMock()
    client.live_stream_manager.subscribe = AsyncMock()
    
    await client.subscribe_live('AAPL', 'position')
    
    client.live_stream_manager.subscribe.assert_called_once_with('AAPL', 'position')


@pytest.mark.asyncio
async def test_unsubscribe_live(client):
    """Test unsubscribe_live delegates to LiveStreamManager."""
    client._connected = True
    client.live_stream_manager = MagicMock()
    client.live_stream_manager.unsubscribe = AsyncMock()
    
    await client.unsubscribe_live('AAPL')
    
    client.live_stream_manager.unsubscribe.assert_called_once_with('AAPL')


def test_get_health_status_not_connected(client):
    """Test get_health_status when not connected."""
    client._connected = False
    
    status = client.get_health_status()
    
    assert status['is_connected'] is False
    assert status['connection_state'] == 'disconnected'
    assert status['status_level'] == 'unhealthy'
    assert status['is_healthy'] is False


def test_get_health_status_connected(client):
    """Test get_health_status when connected."""
    client._connected = True
    
    # Mock components
    client.connection_manager = MagicMock()
    client.connection_manager.is_connected = MagicMock(return_value=True)
    
    client.cache = MagicMock()
    client.cache.get_cache_stats = MagicMock(return_value={
        'total_symbols': 100,
        'total_candles': 20000,
        'memory_mb': 50.0
    })
    
    client.rotating_scanner = MagicMock()
    client.rotating_scanner.get_rotation_status = MagicMock(return_value={
        'current_batch_index': 2,
        'rotation_cycle_count': 5,
        'last_cycle_start': datetime.now(timezone.utc).isoformat()
    })
    
    client.historical_fetcher = MagicMock()
    client.historical_fetcher.get_stats = MagicMock(return_value={
        'total_requests': 100,
        'failed_requests': 2,
        'successful_requests': 98
    })
    
    client.live_stream_manager = MagicMock()
    client.live_stream_manager.get_subscription_count = MagicMock(return_value=15)
    
    # Get health status
    status = client.get_health_status()
    
    # Verify
    assert status['is_connected'] is True
    assert status['connection_state'] == 'connected'
    assert status['active_live_streams'] == 15
    assert status['cached_symbols'] == 100
    assert status['cached_candles'] == 20000
    assert status['status_level'] == 'healthy'


def test_get_metrics(client):
    """Test get_metrics aggregates component metrics."""
    client._connected = True
    
    # Mock components
    client.historical_fetcher = MagicMock()
    client.historical_fetcher.get_stats = MagicMock(return_value={
        'total_requests': 100,
        'failed_requests': 2,
        'successful_requests': 98,
        'average_response_time_ms': 150.5,
        'p95_response_time_ms': 250.0
    })
    
    client.cache = MagicMock()
    client.cache.get_cache_stats = MagicMock(return_value={
        'memory_mb': 50.0
    })
    
    client.rate_limiter = MagicMock()
    client.rate_limiter.get_stats = MagicMock(return_value={
        'delayed_requests': 5,
        'pacing_violations': 0
    })
    
    client.rotating_scanner = MagicMock()
    client.rotating_scanner.get_rotation_status = MagicMock(return_value={
        'batches_processed_this_cycle': 10,
        'symbols_processed_this_cycle': 300,
        'last_cycle_duration_seconds': 600
    })
    
    client.live_stream_manager = MagicMock()
    client.live_stream_manager.get_subscription_count = MagicMock(return_value=15)
    
    # Get metrics
    metrics = client.get_metrics()
    
    # Verify
    assert metrics['total_requests'] == 100
    assert metrics['failed_requests'] == 2
    assert metrics['successful_requests'] == 98
    assert metrics['average_response_time_ms'] == 150.5
    assert metrics['p95_response_time_ms'] == 250.0
    assert metrics['cache_memory_mb'] == 50.0
    assert metrics['rate_limit_delays'] == 5
    assert metrics['pacing_violations'] == 0
    assert metrics['active_subscriptions'] == 15


def test_set_asset_universe(client):
    """Test set_asset_universe updates rotating scanner."""
    client.rotating_scanner = MagicMock()
    
    symbols = ['AAPL', 'MSFT', 'GOOGL']
    client.set_asset_universe(symbols)
    
    assert client._asset_universe == symbols
    client.rotating_scanner.set_asset_universe.assert_called_once_with(symbols)


def test_set_symbol_priority(client):
    """Test set_symbol_priority delegates to rotating scanner."""
    client.rotating_scanner = MagicMock()
    
    client.set_symbol_priority('AAPL', 100)
    
    client.rotating_scanner.set_priority.assert_called_once_with('AAPL', 100)
