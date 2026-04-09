"""
Validation script for IBKR data integration with SignalEngine.

Tests:
1. IBKR data flows correctly to SignalEngine.evaluate()
2. Signal generation works with IBKR-sourced data
3. Alligator, Vortex, Stochastic indicators produce expected signals
4. Data format compatibility between IBKR and existing sources
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.data.market_data import get_latest_candles, get_historical_ohlcv
from src.signals.signal_engine import SignalEngine
from src.indicators.alligator import calculate_alligator, check_alligator_buy, check_alligator_sell
from src.indicators.vortex import calculate_vortex, check_vortex_buy, check_vortex_sell
from src.indicators.stochastic import calculate_stochastic, check_stochastic_buy, check_stochastic_sell

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


def validate_dataframe_format(df: pd.DataFrame, source: str) -> bool:
    """Validate that DataFrame has correct OHLCV format."""
    required_cols = ['time', 'open', 'high', 'low', 'close', 'volume']
    
    if df.empty:
        log.error(f"{source}: DataFrame is empty")
        return False
    
    for col in required_cols:
        if col not in df.columns:
            log.error(f"{source}: Missing column '{col}'")
            return False
    
    # Check data types
    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        log.error(f"{source}: 'time' column is not datetime type")
        return False
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if not pd.api.types.is_numeric_dtype(df[col]):
            log.error(f"{source}: '{col}' column is not numeric type")
            return False
    
    # Check for NaN values
    if df[required_cols].isnull().any().any():
        log.warning(f"{source}: DataFrame contains NaN values")
    
    log.info(f"{source}: DataFrame format is valid ({len(df)} candles)")
    return True


def compare_candles(df1: pd.DataFrame, df2: pd.DataFrame, source1: str, source2: str, tolerance: float = 0.01):
    """Compare candles from two sources for consistency."""
    if df1.empty or df2.empty:
        log.warning(f"Cannot compare: one or both DataFrames are empty")
        return
    
    # Find overlapping time range
    start_time = max(df1['time'].min(), df2['time'].min())
    end_time = min(df1['time'].max(), df2['time'].max())
    
    df1_overlap = df1[(df1['time'] >= start_time) & (df1['time'] <= end_time)]
    df2_overlap = df2[(df2['time'] >= start_time) & (df2['time'] <= end_time)]
    
    if len(df1_overlap) == 0 or len(df2_overlap) == 0:
        log.warning(f"No overlapping time range between {source1} and {source2}")
        return
    
    log.info(f"Comparing {len(df1_overlap)} candles from {source1} vs {len(df2_overlap)} from {source2}")
    
    # Compare latest candle
    latest1 = df1_overlap.iloc[-1]
    latest2 = df2_overlap.iloc[-1]
    
    log.info(f"\nLatest candle comparison:")
    log.info(f"{source1}: time={latest1['time']}, close={latest1['close']:.4f}")
    log.info(f"{source2}: time={latest2['time']}, close={latest2['close']:.4f}")
    
    # Calculate price differences
    for col in ['open', 'high', 'low', 'close']:
        if latest1[col] > 0:
            diff_pct = abs(latest1[col] - latest2[col]) / latest1[col] * 100
            if diff_pct > tolerance:
                log.warning(f"{col}: {diff_pct:.2f}% difference (exceeds {tolerance}% tolerance)")
            else:
                log.info(f"{col}: {diff_pct:.4f}% difference (within tolerance)")


def test_indicator_calculations(df: pd.DataFrame, source: str):
    """Test that indicators calculate correctly on the data."""
    log.info(f"\nTesting indicators on {source} data...")
    
    try:
        # Test Alligator
        alligator_df = calculate_alligator(df)
        has_alligator = not alligator_df[['jaw', 'teeth', 'lips']].isnull().all().all()
        log.info(f"Alligator: {'✓' if has_alligator else '✗'} (calculated)")
        
        if has_alligator and len(alligator_df) >= 2:
            buy_signal = check_alligator_buy(alligator_df)
            sell_signal = check_alligator_sell(alligator_df)
            log.info(f"  Buy signal: {buy_signal}, Sell signal: {sell_signal}")
        
        # Test Vortex
        vortex_df = calculate_vortex(df)
        has_vortex = not vortex_df[['vi_plus', 'vi_minus']].isnull().all().all()
        log.info(f"Vortex: {'✓' if has_vortex else '✗'} (calculated)")
        
        if has_vortex and len(vortex_df) >= 2:
            buy_signal = check_vortex_buy(vortex_df)
            sell_signal = check_vortex_sell(vortex_df)
            log.info(f"  Buy signal: {buy_signal}, Sell signal: {sell_signal}")
        
        # Test Stochastic
        stoch_df = calculate_stochastic(df)
        has_stoch = not stoch_df[['stoch_k', 'stoch_d']].isnull().all().all()
        log.info(f"Stochastic: {'✓' if has_stoch else '✗'} (calculated)")
        
        if has_stoch and len(stoch_df) >= 2:
            buy_signal = check_stochastic_buy(stoch_df)
            sell_signal = check_stochastic_sell(stoch_df)
            log.info(f"  Buy signal: {buy_signal}, Sell signal: {sell_signal}")
        
        return has_alligator and has_vortex and has_stoch
        
    except Exception as e:
        log.error(f"Indicator calculation failed: {e}", exc_info=True)
        return False


def test_signal_engine(df: pd.DataFrame, symbol: str, timeframe: str, source: str):
    """Test SignalEngine with the data."""
    log.info(f"\nTesting SignalEngine on {source} data for {symbol} {timeframe}...")
    
    try:
        engine = SignalEngine(symbol, timeframe)
        result = engine.evaluate(df)
        
        buy_signal = result.get('buy')
        sell_signal = result.get('sell')
        conflict = result.get('conflict', False)
        
        log.info(f"SignalEngine evaluation complete:")
        log.info(f"  Buy signal valid: {buy_signal.is_valid if buy_signal else False}")
        log.info(f"  Sell signal valid: {sell_signal.is_valid if sell_signal else False}")
        log.info(f"  Conflict detected: {conflict}")
        
        if buy_signal and buy_signal.is_valid:
            log.info(f"  Buy signal details:")
            log.info(f"    Entry: {buy_signal.entry_price:.4f}")
            log.info(f"    Stop Loss: {buy_signal.stop_loss:.4f}")
            log.info(f"    Alligator: {buy_signal.alligator_point}")
            log.info(f"    Vortex: {buy_signal.vortex_point}")
            log.info(f"    Stochastic: {buy_signal.stochastic_point}")
        
        if sell_signal and sell_signal.is_valid:
            log.info(f"  Sell signal details:")
            log.info(f"    Entry: {sell_signal.entry_price:.4f}")
            log.info(f"    Stop Loss: {sell_signal.stop_loss:.4f}")
            log.info(f"    Alligator: {sell_signal.alligator_point}")
            log.info(f"    Vortex: {sell_signal.vortex_point}")
            log.info(f"    Stochastic: {sell_signal.stochastic_point}")
        
        return True
        
    except Exception as e:
        log.error(f"SignalEngine evaluation failed: {e}", exc_info=True)
        return False


def main():
    """Run validation tests."""
    log.info("=" * 80)
    log.info("IBKR Data Integration Validation")
    log.info("=" * 80)
    
    # Test symbols (use small watchlist)
    test_symbols = [
        ("AAPL", "stock"),      # Stock
        ("EUR/USD", "forex"),   # Forex
        ("BTC/USD", "crypto"),  # Crypto
    ]
    
    timeframe = "1h"
    count = 200
    
    for symbol, asset_class in test_symbols:
        log.info(f"\n{'=' * 80}")
        log.info(f"Testing {symbol} ({asset_class})")
        log.info(f"{'=' * 80}")
        
        # Test 1: Fetch data from IBKR
        log.info(f"\n1. Fetching data from IBKR...")
        try:
            ibkr_df = get_latest_candles(symbol, timeframe, count, source="ibkr")
            if validate_dataframe_format(ibkr_df, "IBKR"):
                log.info(f"✓ IBKR data fetch successful")
            else:
                log.error(f"✗ IBKR data format validation failed")
                continue
        except Exception as e:
            log.error(f"✗ IBKR data fetch failed: {e}")
            continue
        
        # Test 2: Fetch data from existing source for comparison
        log.info(f"\n2. Fetching data from existing source for comparison...")
        try:
            if asset_class == "crypto":
                compare_source = "ccxt"
            elif asset_class == "forex":
                compare_source = "finnhub"
            else:
                compare_source = "yfinance"
            
            compare_df = get_latest_candles(symbol, timeframe, count, source=compare_source)
            if validate_dataframe_format(compare_df, compare_source):
                log.info(f"✓ {compare_source} data fetch successful")
                
                # Compare candles
                compare_candles(ibkr_df, compare_df, "IBKR", compare_source)
            else:
                log.warning(f"✗ {compare_source} data format validation failed")
        except Exception as e:
            log.warning(f"✗ {compare_source} data fetch failed: {e}")
        
        # Test 3: Test indicator calculations
        log.info(f"\n3. Testing indicator calculations...")
        if test_indicator_calculations(ibkr_df, "IBKR"):
            log.info(f"✓ All indicators calculated successfully")
        else:
            log.error(f"✗ Indicator calculation failed")
        
        # Test 4: Test SignalEngine
        log.info(f"\n4. Testing SignalEngine integration...")
        if test_signal_engine(ibkr_df, symbol, timeframe, "IBKR"):
            log.info(f"✓ SignalEngine integration successful")
        else:
            log.error(f"✗ SignalEngine integration failed")
    
    log.info(f"\n{'=' * 80}")
    log.info("Validation complete")
    log.info(f"{'=' * 80}")


if __name__ == "__main__":
    main()
