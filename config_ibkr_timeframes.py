"""
IBKR Timeframe Configuration

Configures the bot to use larger timeframes (15m, 30m, 1h, 2h, 4h) for IBKR trading.
Excludes smaller timeframes (1m, 3m, 5m) to focus on longer-term trades.

This configuration ensures:
1. All markets get scanned eventually through rotation
2. API load is manageable
3. Focus on quality longer-term setups
4. Better risk/reward ratios
"""

# Supported timeframes for IBKR trading (larger timeframes only)
IBKR_TIMEFRAMES = [
    "15m",  # 15 minutes
    "30m",  # 30 minutes
    "1h",   # 1 hour
    "2h",   # 2 hours
    "4h",   # 4 hours
]

# Primary timeframe for scanning (recommended: 1h for balance of frequency and quality)
PRIMARY_TIMEFRAME = "1h"

# Rotation configuration for comprehensive market coverage
ROTATION_CONFIG = {
    # Batch size: number of symbols to scan per batch
    # Smaller = less API load, slower full rotation
    # Larger = faster full rotation, more API load
    "batch_size": 30,
    
    # Delay between batches (seconds)
    # Gives IBKR API time to process requests
    "rotation_delay_seconds": 10,
    
    # Target full rotation cycle time (minutes)
    # All symbols should be scanned within this time
    "rotation_cycle_minutes": 10,
    
    # Maximum concurrent live subscriptions
    # IBKR limit is ~100, we use 80 for safety
    "max_live_subscriptions": 80,
}

# Timeframe-specific candle counts
# Larger timeframes need more historical candles for indicator calculation
TIMEFRAME_CANDLE_COUNTS = {
    "15m": 200,  # ~50 hours of data
    "30m": 200,  # ~100 hours of data
    "1h": 200,   # ~8 days of data
    "2h": 200,   # ~16 days of data
    "4h": 200,   # ~33 days of data
}

# Multi-timeframe scanning configuration
# Scan multiple timeframes for each symbol to catch different trend speeds
MULTI_TIMEFRAME_ENABLED = False  # Set to True to enable multi-timeframe scanning

# If multi-timeframe enabled, these timeframes will be scanned for each symbol
MULTI_TIMEFRAMES = [
    "1h",   # Short-term trends
    "4h",   # Medium-term trends
]

# Priority symbols (always scanned first in each rotation)
# Add symbols with open positions or recent signals here
PRIORITY_SYMBOLS = []

# Symbol universe configuration
# Set to None to use all symbols from symbol_mapper
# Or provide a custom list of symbols to scan
SYMBOL_UNIVERSE = None  # Will use get_all_symbols() from symbol_mapper

# Timeframe rotation strategy
# "sequential": Scan one timeframe completely before moving to next
# "parallel": Scan all timeframes for each symbol before moving to next symbol
TIMEFRAME_ROTATION_STRATEGY = "sequential"

# Estimated scan times (for planning)
# Based on batch_size=30, rotation_delay=10s
ESTIMATED_SCAN_TIMES = {
    "50 symbols": "~2 minutes per timeframe",
    "100 symbols": "~4 minutes per timeframe",
    "200 symbols": "~8 minutes per timeframe",
    "500 symbols": "~20 minutes per timeframe",
}

# Notes:
# - With 30 symbols per batch and 10s delay, we can scan ~180 symbols per 10 minutes
# - This ensures all markets get scanned within the rotation cycle
# - Adjust batch_size and rotation_delay based on your symbol count
# - Monitor rate limit violations and adjust if needed


def get_timeframe_config():
    """Get timeframe configuration for IBKR scanning."""
    return {
        "timeframes": IBKR_TIMEFRAMES,
        "primary_timeframe": PRIMARY_TIMEFRAME,
        "rotation_config": ROTATION_CONFIG,
        "candle_counts": TIMEFRAME_CANDLE_COUNTS,
        "multi_timeframe_enabled": MULTI_TIMEFRAME_ENABLED,
        "multi_timeframes": MULTI_TIMEFRAMES,
        "priority_symbols": PRIORITY_SYMBOLS,
        "symbol_universe": SYMBOL_UNIVERSE,
        "rotation_strategy": TIMEFRAME_ROTATION_STRATEGY,
    }


def validate_timeframe(timeframe: str) -> bool:
    """Check if timeframe is supported for IBKR trading."""
    return timeframe in IBKR_TIMEFRAMES


def get_candle_count(timeframe: str) -> int:
    """Get recommended candle count for a timeframe."""
    return TIMEFRAME_CANDLE_COUNTS.get(timeframe, 200)


if __name__ == "__main__":
    # Print configuration summary
    print("=" * 80)
    print("IBKR Timeframe Configuration")
    print("=" * 80)
    print(f"\nSupported Timeframes: {', '.join(IBKR_TIMEFRAMES)}")
    print(f"Primary Timeframe: {PRIMARY_TIMEFRAME}")
    print(f"\nRotation Configuration:")
    print(f"  Batch Size: {ROTATION_CONFIG['batch_size']} symbols")
    print(f"  Rotation Delay: {ROTATION_CONFIG['rotation_delay_seconds']} seconds")
    print(f"  Cycle Time: {ROTATION_CONFIG['rotation_cycle_minutes']} minutes")
    print(f"  Max Live Subscriptions: {ROTATION_CONFIG['max_live_subscriptions']}")
    print(f"\nMulti-Timeframe Scanning: {'Enabled' if MULTI_TIMEFRAME_ENABLED else 'Disabled'}")
    if MULTI_TIMEFRAME_ENABLED:
        print(f"  Timeframes: {', '.join(MULTI_TIMEFRAMES)}")
    print(f"\nEstimated Scan Times:")
    for symbol_count, time_estimate in ESTIMATED_SCAN_TIMES.items():
        print(f"  {symbol_count}: {time_estimate}")
    print("\n" + "=" * 80)
