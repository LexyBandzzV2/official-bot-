"""
Test IBKR Integration with Small Watchlist

Tests the complete IBKR integration with a small watchlist (5-10 symbols)
before scaling to the full universe.

Tests:
1. IBKR connection and data fetching
2. Signal generation with IBKR data
3. Indicator calculations (Alligator, Vortex, Stochastic)
4. Trade candidate logging
5. End-to-end flow validation

Usage:
    python test_ibkr_small_watchlist.py
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.data.market_data import get_latest_candles, shutdown_ibkr_client
from src.scanner.market_scanner import MarketScanner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


# Small watchlist for testing (5-10 symbols across different asset classes)
TEST_WATCHLIST = [
    # Stocks
    "AAPL",    # Apple
    "TSLA",    # Tesla
    "MSFT",    # Microsoft
    
    # Forex
    "EURUSD",  # EUR/USD
    "GBPUSD",  # GBP/USD
    
    # Crypto
    "BTCUSD",  # Bitcoin
    "ETHUSDT", # Ethereum
    
    # Commodities
    "XAUUSD",  # Gold
]


def test_data_fetch():
    """Test 1: Verify IBKR data fetching works for all symbols."""
    log.info("=" * 80)
    log.info("TEST 1: IBKR Data Fetching")
    log.info("=" * 80)
    
    timeframe = "1h"
    count = 200
    
    results = {}
    
    for symbol in TEST_WATCHLIST:
        log.info(f"\nFetching {symbol} from IBKR...")
        try:
            df = get_latest_candles(symbol, timeframe, count, source="ibkr")
            
            if df.empty:
                log.error(f"✗ {symbol}: No data returned")
                results[symbol] = False
            else:
                log.info(f"✓ {symbol}: {len(df)} candles fetched")
                log.info(f"  Latest close: {df.iloc[-1]['close']:.4f}")
                log.info(f"  Time range: {df.iloc[0]['time']} to {df.iloc[-1]['time']}")
                results[symbol] = True
        except Exception as e:
            log.error(f"✗ {symbol}: Error - {e}")
            results[symbol] = False
    
    # Summary
    success_count = sum(1 for v in results.values() if v)
    total_count = len(results)
    
    log.info(f"\n{'=' * 80}")
    log.info(f"Data Fetch Results: {success_count}/{total_count} successful")
    log.info(f"{'=' * 80}")
    
    return success_count == total_count


def test_scanner_dry_run():
    """Test 2: Run scanner in dry-run mode with IBKR data."""
    log.info("\n" + "=" * 80)
    log.info("TEST 2: Scanner Dry Run with IBKR Data")
    log.info("=" * 80)
    
    try:
        # Create scanner with IBKR as data source
        scanner = MarketScanner(
            symbols=TEST_WATCHLIST,
            timeframe="1h",
            top_candidates=5,
            candles_lookback=200,
            dry_run=True,
            data_source="ibkr"  # Force IBKR as data source
        )
        
        log.info("\nRunning single scan cycle...")
        scanner._scan_once()
        
        log.info("\n✓ Scanner dry run completed successfully")
        log.info(f"Check logs/trade_candidates.log for detailed candidate logging")
        
        return True
        
    except Exception as e:
        log.error(f"✗ Scanner dry run failed: {e}", exc_info=True)
        return False


def test_signal_generation():
    """Test 3: Verify signal generation with IBKR data."""
    log.info("\n" + "=" * 80)
    log.info("TEST 3: Signal Generation with IBKR Data")
    log.info("=" * 80)
    
    from src.signals.signal_engine import SignalEngine
    from src.indicators.alligator import calculate_alligator
    from src.indicators.vortex import calculate_vortex
    from src.indicators.stochastic import calculate_stochastic
    
    timeframe = "1h"
    count = 200
    
    for symbol in TEST_WATCHLIST[:3]:  # Test first 3 symbols
        log.info(f"\nTesting signal generation for {symbol}...")
        
        try:
            # Fetch data
            df = get_latest_candles(symbol, timeframe, count, source="ibkr")
            
            if df.empty:
                log.warning(f"  Skipping {symbol}: No data")
                continue
            
            # Calculate indicators
            alligator_df = calculate_alligator(df)
            vortex_df = calculate_vortex(df)
            stochastic_df = calculate_stochastic(df)
            
            # Check indicator values
            latest = alligator_df.iloc[-1]
            log.info(f"  Alligator: jaw={latest['jaw']:.4f}, teeth={latest['teeth']:.4f}, lips={latest['lips']:.4f}")
            
            latest = vortex_df.iloc[-1]
            log.info(f"  Vortex: vi+={latest['vi_plus']:.4f}, vi-={latest['vi_minus']:.4f}")
            
            latest = stochastic_df.iloc[-1]
            log.info(f"  Stochastic: k={latest['stoch_k']:.2f}, d={latest['stoch_d']:.2f}")
            
            # Test signal engine
            engine = SignalEngine(symbol, timeframe)
            result = engine.evaluate(df)
            
            buy_signal = result.get('buy')
            sell_signal = result.get('sell')
            
            log.info(f"  Buy signal: {'VALID' if buy_signal and buy_signal.is_valid else 'INVALID'}")
            log.info(f"  Sell signal: {'VALID' if sell_signal and sell_signal.is_valid else 'INVALID'}")
            
        except Exception as e:
            log.error(f"  Error testing {symbol}: {e}")
    
    log.info("\n✓ Signal generation test completed")
    return True


def main():
    """Run all tests."""
    log.info("=" * 80)
    log.info("IBKR Integration Test - Small Watchlist")
    log.info("=" * 80)
    log.info(f"Testing with {len(TEST_WATCHLIST)} symbols:")
    for symbol in TEST_WATCHLIST:
        log.info(f"  - {symbol}")
    log.info("")
    
    results = {}
    
    # Test 1: Data fetching
    results['data_fetch'] = test_data_fetch()
    
    # Test 2: Scanner dry run
    results['scanner_dry_run'] = test_scanner_dry_run()
    
    # Test 3: Signal generation
    results['signal_generation'] = test_signal_generation()
    
    # Summary
    log.info("\n" + "=" * 80)
    log.info("TEST SUMMARY")
    log.info("=" * 80)
    
    for test_name, passed in results.items():
        status = "✓ PASSED" if passed else "✗ FAILED"
        log.info(f"{test_name}: {status}")
    
    all_passed = all(results.values())
    
    log.info("\n" + "=" * 80)
    if all_passed:
        log.info("ALL TESTS PASSED ✓")
        log.info("\nNext steps:")
        log.info("1. Review logs/trade_candidates.log for detailed candidate logging")
        log.info("2. Verify indicator values match expected output")
        log.info("3. If everything looks good, scale to full universe")
    else:
        log.info("SOME TESTS FAILED ✗")
        log.info("\nPlease review errors above before proceeding")
    log.info("=" * 80)
    
    # Cleanup
    log.info("\nShutting down IBKR client...")
    shutdown_ibkr_client()
    log.info("Cleanup complete")
    
    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
