"""
Unit tests for OHLCVCache.

Tests cache storage, retrieval, pruning, persistence, and live updates.
"""

import asyncio
import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import shutil

from src.data.ibkr.cache import OHLCVCache, CacheKey


class TestCacheKey:
    """Test suite for CacheKey."""
    
    def test_cache_key_creation(self):
        """Test CacheKey initialization."""
        key = CacheKey("AAPL", "1h")
        assert key.symbol == "AAPL"
        assert key.timeframe == "1h"
    
    def test_cache_key_string(self):
        """Test CacheKey string representation."""
        key = CacheKey("BTCUSD", "5m")
        assert str(key) == "BTCUSD_5m"
    
    def test_cache_key_filename(self):
        """Test CacheKey filename generation."""
        key = CacheKey("EURUSD", "15m")
        assert key.to_filename() == "EURUSD_15m_ibkr.parquet"
    
    def test_cache_key_hashable(self):
        """Test CacheKey can be used as dict key."""
        key1 = CacheKey("AAPL", "1h")
        key2 = CacheKey("AAPL", "1h")
        key3 = CacheKey("AAPL", "5m")
        
        cache_dict = {key1: "value1"}
        assert cache_dict[key2] == "value1"  # Same key
        assert key3 not in cache_dict  # Different key


class TestOHLCVCache:
    """Test suite for OHLCVCache."""
    
    @pytest.fixture
    def cache(self):
        """Create OHLCVCache instance for testing."""
        return OHLCVCache(retention_hours=24)
    
    @pytest.fixture
    def sample_candles(self):
        """Create sample OHLCV data."""
        now = datetime.now(timezone.utc)
        data = []
        for i in range(10):
            timestamp = now - timedelta(minutes=10 - i)
            data.append({
                'time': timestamp,
                'open': 100.0 + i,
                'high': 105.0 + i,
                'low': 95.0 + i,
                'close': 102.0 + i,
                'volume': 1000.0 + i * 10
            })
        return pd.DataFrame(data)
    
    def test_initialization(self, cache):
        """Test OHLCVCache initialization."""
        assert cache.retention_hours == 24
        assert len(cache._cache) == 0
    
    def test_get_empty_cache(self, cache):
        """Test get returns empty DataFrame for missing key."""
        df = cache.get("AAPL", "1h")
        assert df.empty
        assert list(df.columns) == ['time', 'open', 'high', 'low', 'close', 'volume']
    
    def test_update_and_get(self, cache, sample_candles):
        """Test updating cache and retrieving data."""
        cache.update("AAPL", "1h", sample_candles)
        
        df = cache.get("AAPL", "1h")
        assert len(df) == 10
        assert list(df.columns) == ['time', 'open', 'high', 'low', 'close', 'volume']
        pd.testing.assert_frame_equal(df, sample_candles)
    
    def test_get_with_count_limit(self, cache, sample_candles):
        """Test get returns only requested number of candles."""
        cache.update("AAPL", "1h", sample_candles)
        
        df = cache.get("AAPL", "1h", count=5)
        assert len(df) == 5
        # Should return most recent 5 candles
        pd.testing.assert_frame_equal(df, sample_candles.tail(5).reset_index(drop=True))
    
    def test_update_empty_dataframe(self, cache):
        """Test updating with empty DataFrame is handled gracefully."""
        empty_df = pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        cache.update("AAPL", "1h", empty_df)
        
        # Cache should remain empty
        df = cache.get("AAPL", "1h")
        assert df.empty

    def test_update_merges_with_existing(self, cache):
        """Test updating cache merges with existing data."""
        now = datetime.now(timezone.utc)
        
        # First batch
        df1 = pd.DataFrame([
            {
                'time': now - timedelta(minutes=5),
                'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            },
            {
                'time': now - timedelta(minutes=4),
                'open': 102.0, 'high': 107.0, 'low': 97.0, 'close': 104.0, 'volume': 1100.0
            }
        ])
        cache.update("AAPL", "1h", df1)
        
        # Second batch with overlap
        df2 = pd.DataFrame([
            {
                'time': now - timedelta(minutes=4),  # Duplicate
                'open': 102.5, 'high': 108.0, 'low': 98.0, 'close': 105.0, 'volume': 1200.0
            },
            {
                'time': now - timedelta(minutes=3),  # New
                'open': 105.0, 'high': 110.0, 'low': 100.0, 'close': 107.0, 'volume': 1300.0
            }
        ])
        cache.update("AAPL", "1h", df2)
        
        df = cache.get("AAPL", "1h")
        assert len(df) == 3  # 2 unique + 1 new
        
        # Check duplicate was replaced with most recent
        row = df[df['time'] == now - timedelta(minutes=4)].iloc[0]
        assert row['close'] == 105.0  # From df2, not df1
    
    def test_update_sorts_by_time(self, cache):
        """Test cache data is sorted by time."""
        now = datetime.now(timezone.utc)
        
        # Insert in reverse order
        df = pd.DataFrame([
            {
                'time': now - timedelta(minutes=1),
                'open': 103.0, 'high': 108.0, 'low': 98.0, 'close': 105.0, 'volume': 1200.0
            },
            {
                'time': now - timedelta(minutes=5),
                'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            },
            {
                'time': now - timedelta(minutes=3),
                'open': 102.0, 'high': 107.0, 'low': 97.0, 'close': 104.0, 'volume': 1100.0
            }
        ])
        cache.update("AAPL", "1h", df)
        
        result = cache.get("AAPL", "1h")
        # Check times are sorted
        times = result['time'].tolist()
        assert times == sorted(times)
    
    def test_validate_candles_filters_invalid(self, cache):
        """Test invalid candles are filtered out."""
        now = datetime.now(timezone.utc)
        
        df = pd.DataFrame([
            # Valid candle
            {
                'time': now - timedelta(minutes=5),
                'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            },
            # Invalid: high < low
            {
                'time': now - timedelta(minutes=4),
                'open': 100.0, 'high': 90.0, 'low': 95.0, 'close': 92.0, 'volume': 1000.0
            },
            # Invalid: negative price
            {
                'time': now - timedelta(minutes=3),
                'open': -100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            },
            # Valid candle
            {
                'time': now - timedelta(minutes=2),
                'open': 102.0, 'high': 107.0, 'low': 97.0, 'close': 104.0, 'volume': 1100.0
            }
        ])
        cache.update("AAPL", "1h", df)
        
        result = cache.get("AAPL", "1h")
        assert len(result) == 2  # Only 2 valid candles
    
    def test_validate_candles_rejects_future_timestamps(self, cache):
        """Test candles with future timestamps are rejected."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        
        df = pd.DataFrame([
            {
                'time': future,
                'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            }
        ])
        cache.update("AAPL", "1h", df)
        
        result = cache.get("AAPL", "1h")
        assert result.empty
    
    def test_prune_old_candles(self, cache):
        """Test old candles are pruned based on retention window."""
        now = datetime.now(timezone.utc)
        
        df = pd.DataFrame([
            # Old candle (beyond retention)
            {
                'time': now - timedelta(hours=25),
                'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
            },
            # Recent candle (within retention)
            {
                'time': now - timedelta(hours=1),
                'open': 102.0, 'high': 107.0, 'low': 97.0, 'close': 104.0, 'volume': 1100.0
            }
        ])
        cache.update("AAPL", "1h", df)
        
        result = cache.get("AAPL", "1h")
        assert len(result) == 1  # Only recent candle retained
        assert result.iloc[0]['close'] == 104.0
    
    def test_update_current_candle_creates_first(self, cache):
        """Test update_current_candle creates first candle if cache empty."""
        now = datetime.now(timezone.utc)
        cache.update_current_candle("AAPL", "1h", 100.0, 1000.0, now)
        
        df = cache.get("AAPL", "1h")
        assert len(df) == 1
        assert df.iloc[0]['open'] == 100.0
        assert df.iloc[0]['high'] == 100.0
        assert df.iloc[0]['low'] == 100.0
        assert df.iloc[0]['close'] == 100.0
        assert df.iloc[0]['volume'] == 1000.0
    
    def test_update_current_candle_updates_existing(self, cache):
        """Test update_current_candle updates most recent candle."""
        now = datetime.now(timezone.utc)
        
        # Create initial candle
        df = pd.DataFrame([{
            'time': now - timedelta(seconds=30),
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
        }])
        cache.update("AAPL", "1h", df)
        
        # Update with higher price
        cache.update_current_candle("AAPL", "1h", 110.0, 500.0, now)
        
        result = cache.get("AAPL", "1h")
        assert len(result) == 1
        assert result.iloc[0]['close'] == 110.0
        assert result.iloc[0]['high'] == 110.0  # Updated
        assert result.iloc[0]['low'] == 95.0  # Unchanged
        assert result.iloc[0]['volume'] == 1500.0  # Accumulated
    
    def test_update_current_candle_updates_low(self, cache):
        """Test update_current_candle updates low price."""
        now = datetime.now(timezone.utc)
        
        # Create initial candle
        df = pd.DataFrame([{
            'time': now - timedelta(seconds=30),
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
        }])
        cache.update("AAPL", "1h", df)
        
        # Update with lower price
        cache.update_current_candle("AAPL", "1h", 90.0, 500.0, now)
        
        result = cache.get("AAPL", "1h")
        assert result.iloc[0]['low'] == 90.0  # Updated
        assert result.iloc[0]['high'] == 105.0  # Unchanged
    
    def test_update_current_candle_creates_new_period(self, cache):
        """Test update_current_candle creates new candle for new period."""
        now = datetime.now(timezone.utc)
        
        # Create candle 2 hours ago
        df = pd.DataFrame([{
            'time': now - timedelta(hours=2),
            'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
        }])
        cache.update("AAPL", "1h", df)
        
        # Update with current time (new period)
        cache.update_current_candle("AAPL", "1h", 110.0, 500.0, now)
        
        result = cache.get("AAPL", "1h")
        assert len(result) == 2  # New candle created
        assert result.iloc[1]['open'] == 110.0
        assert result.iloc[1]['close'] == 110.0
    
    def test_update_current_candle_ignores_invalid_price(self, cache):
        """Test update_current_candle ignores invalid prices."""
        now = datetime.now(timezone.utc)
        
        # Try to update with negative price
        cache.update_current_candle("AAPL", "1h", -100.0, 1000.0, now)
        
        df = cache.get("AAPL", "1h")
        assert df.empty  # No candle created
    
    def test_get_cache_stats(self, cache, sample_candles):
        """Test get_cache_stats returns correct statistics."""
        cache.update("AAPL", "1h", sample_candles)
        cache.update("BTCUSD", "5m", sample_candles)
        
        stats = cache.get_cache_stats()
        assert stats['total_symbols'] == 2
        assert stats['total_candles'] == 20
        assert stats['retention_hours'] == 24
        assert stats['memory_mb'] > 0

    @pytest.mark.asyncio
    async def test_persist_and_load(self, cache, sample_candles):
        """Test cache persistence to disk and loading."""
        # Create temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            
            # Add data to cache
            cache.update("AAPL", "1h", sample_candles)
            cache.update("BTCUSD", "5m", sample_candles)
            
            # Persist to disk
            await cache.persist(cache_dir)
            
            # Verify files created
            assert (cache_dir / "AAPL_1h_ibkr.parquet").exists()
            assert (cache_dir / "BTCUSD_5m_ibkr.parquet").exists()
            
            # Create new cache and load
            new_cache = OHLCVCache(retention_hours=24)
            await new_cache.load(cache_dir)
            
            # Verify data loaded correctly
            df_aapl = new_cache.get("AAPL", "1h")
            assert len(df_aapl) == 10
            
            df_btc = new_cache.get("BTCUSD", "5m")
            assert len(df_btc) == 10
    
    @pytest.mark.asyncio
    async def test_load_nonexistent_directory(self, cache):
        """Test loading from nonexistent directory is handled gracefully."""
        cache_dir = Path("/nonexistent/directory")
        
        # Should not raise exception
        await cache.load(cache_dir)
        
        # Cache should be empty
        stats = cache.get_cache_stats()
        assert stats['total_symbols'] == 0
    
    @pytest.mark.asyncio
    async def test_load_prunes_stale_data(self):
        """Test loading from disk prunes data older than 7 days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            
            # Create data with old timestamps
            now = datetime.now(timezone.utc)
            old_data = pd.DataFrame([
                {
                    'time': now - timedelta(days=10),  # Too old
                    'open': 100.0, 'high': 105.0, 'low': 95.0, 'close': 102.0, 'volume': 1000.0
                },
                {
                    'time': now - timedelta(days=3),  # Recent enough
                    'open': 102.0, 'high': 107.0, 'low': 97.0, 'close': 104.0, 'volume': 1100.0
                }
            ])
            
            # Use a cache with longer retention to save the old data
            long_retention_cache = OHLCVCache(retention_hours=24 * 15)  # 15 days
            long_retention_cache.update("AAPL", "1h", old_data)
            await long_retention_cache.persist(cache_dir)
            
            # Load in new cache with normal retention
            new_cache = OHLCVCache(retention_hours=24)
            await new_cache.load(cache_dir)
            
            # Should only have recent data (3 days old is within 7-day load threshold)
            df = new_cache.get("AAPL", "1h")
            assert len(df) == 1
            assert df.iloc[0]['close'] == 104.0
    
    @pytest.mark.asyncio
    async def test_load_handles_corrupted_files(self, cache):
        """Test loading handles corrupted cache files gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            
            # Create corrupted file
            corrupted_file = cache_dir / "AAPL_1h_ibkr.parquet"
            corrupted_file.write_text("corrupted data")
            
            # Should not raise exception
            await cache.load(cache_dir)
            
            # Cache should be empty
            df = cache.get("AAPL", "1h")
            assert df.empty
    
    @pytest.mark.asyncio
    async def test_persist_empty_cache(self, cache):
        """Test persisting empty cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            
            # Persist empty cache
            await cache.persist(cache_dir)
            
            # Directory should be created but no files
            assert cache_dir.exists()
            assert len(list(cache_dir.glob("*.parquet"))) == 0
    
    def test_thread_safety_concurrent_reads(self, cache, sample_candles):
        """Test cache handles concurrent read operations safely."""
        import threading
        
        cache.update("AAPL", "1h", sample_candles)
        
        results = []
        errors = []
        
        def read_cache():
            try:
                df = cache.get("AAPL", "1h")
                results.append(len(df))
            except Exception as e:
                errors.append(e)
        
        # Create multiple threads reading concurrently
        threads = [threading.Thread(target=read_cache) for _ in range(10)]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # All reads should succeed
        assert len(errors) == 0
        assert len(results) == 10
        assert all(r == 10 for r in results)
    
    def test_thread_safety_concurrent_writes(self, cache):
        """Test cache handles concurrent write operations safely."""
        import threading
        
        now = datetime.now(timezone.utc)
        
        def write_cache(thread_id):
            df = pd.DataFrame([{
                'time': now - timedelta(minutes=thread_id),
                'open': 100.0 + thread_id,
                'high': 105.0 + thread_id,
                'low': 95.0 + thread_id,
                'close': 102.0 + thread_id,
                'volume': 1000.0
            }])
            cache.update("AAPL", "1h", df)
        
        # Create multiple threads writing concurrently
        threads = [threading.Thread(target=write_cache, args=(i,)) for i in range(10)]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # All writes should be merged
        df = cache.get("AAPL", "1h")
        assert len(df) == 10
    
    def test_multiple_symbols_and_timeframes(self, cache, sample_candles):
        """Test cache handles multiple symbols and timeframes independently."""
        cache.update("AAPL", "1h", sample_candles)
        cache.update("AAPL", "5m", sample_candles)
        cache.update("BTCUSD", "1h", sample_candles)
        
        # Each should be independent
        assert len(cache.get("AAPL", "1h")) == 10
        assert len(cache.get("AAPL", "5m")) == 10
        assert len(cache.get("BTCUSD", "1h")) == 10
        assert len(cache.get("BTCUSD", "5m")) == 0  # Not added
        
        stats = cache.get_cache_stats()
        assert stats['total_symbols'] == 3
        assert stats['total_candles'] == 30


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
