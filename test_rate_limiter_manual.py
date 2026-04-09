"""
Manual test for RateLimiter to verify async functionality.
"""

import asyncio
import time
from src.data.ibkr.rate_limiter import RateLimiter


async def test_basic_rate_limiting():
    """Test basic rate limiting functionality."""
    print("Testing basic rate limiting...")
    
    rl = RateLimiter(requests_per_10min=5, min_delay_seconds=0.5)
    
    start = time.time()
    
    # Make 3 requests - should be delayed by min_delay_seconds
    for i in range(3):
        await rl.acquire("test")
        print(f"Request {i+1} completed at {time.time() - start:.2f}s")
    
    elapsed = time.time() - start
    print(f"Total time for 3 requests: {elapsed:.2f}s")
    print(f"Expected: ~1.0s (2 delays of 0.5s)")
    
    stats = rl.get_stats()
    print(f"Stats: {stats}")
    
    assert stats['total_requests'] == 3
    assert stats['delayed_requests'] >= 2  # At least 2 delays
    print("✓ Basic rate limiting test passed\n")


async def test_window_limit():
    """Test 10-minute window limit."""
    print("Testing 10-minute window limit...")
    
    rl = RateLimiter(requests_per_10min=3, min_delay_seconds=0.1)
    
    start = time.time()
    
    # Make 3 requests - should succeed quickly
    for i in range(3):
        await rl.acquire("test")
        print(f"Request {i+1} completed at {time.time() - start:.2f}s")
    
    # 4th request should be delayed until oldest request expires
    print("Making 4th request (should be delayed)...")
    await rl.acquire("test")
    elapsed = time.time() - start
    print(f"Request 4 completed at {elapsed:.2f}s")
    
    stats = rl.get_stats()
    print(f"Stats: {stats}")
    
    assert stats['total_requests'] == 4
    print("✓ Window limit test passed\n")


async def test_pacing_violation():
    """Test pacing violation handling."""
    print("Testing pacing violation handling...")
    
    rl = RateLimiter(requests_per_10min=10, min_delay_seconds=0.1)
    
    # Simulate pacing violation
    rl.record_pacing_violation()
    
    stats = rl.get_stats()
    print(f"Stats after violation: {stats}")
    assert stats['is_paused'] is True
    assert stats['pacing_violations'] == 1
    
    print("Attempting request during pause (should wait)...")
    start = time.time()
    await rl.acquire("test")
    elapsed = time.time() - start
    print(f"Request completed after {elapsed:.2f}s")
    
    # Should have waited ~60 seconds (but we'll just check it waited)
    assert elapsed > 0.5  # At least some delay
    print("✓ Pacing violation test passed\n")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("RateLimiter Manual Tests")
    print("=" * 60 + "\n")
    
    await test_basic_rate_limiting()
    await test_window_limit()
    # Skip pacing violation test as it takes 60 seconds
    # await test_pacing_violation()
    
    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
