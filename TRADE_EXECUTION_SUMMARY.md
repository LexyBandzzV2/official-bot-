# Trade Execution Parameters - IBKR Integration

## Overview

This document describes the complete trade execution setup for IBKR, including stop loss, take profit, and exit conditions based on your requirements.

## Execution Parameters

### 1. Stop Loss (Trails Red Line - Alligator Teeth)

**Initial Stop Loss**: 2% from entry price
- **BUY trades**: Entry × 0.98 (2% below entry)
- **SELL trades**: Entry × 1.02 (2% above entry)

**Trailing Behavior**:
- Stop loss trails the **red line (Alligator teeth)**
- Only moves in favorable direction (never against you)
- **BUY**: Stop rises as teeth rises (locks in profit)
- **SELL**: Stop falls as teeth falls (locks in profit)
- Hard floor at 2% ensures minimum protection

**Implementation**: `src/risk/trailing_stop.py`

### 2. Take Profit (Trails Green Line - Alligator Lips)

**Initial Take Profit**: Set at current lips (green line) price

**Trailing Behavior**:
- Take profit trails the **green line (Alligator lips)**
- Only activates after 1% minimum profit is reached
- Only moves in favorable direction (never against you)
- **BUY**: TP rises as lips rises (follows momentum)
- **SELL**: TP falls as lips falls (follows momentum)
- Allows winners to run while protecting against reversals

**Implementation**: `src/risk/alligator_trailing_tp.py`

### 3. Exit Conditions

**Primary Exit**: Green line touches red line
- When lips (green) touches teeth (red) after trade entry
- Signals momentum reversal
- Automatic exit regardless of profit/loss
- **Implementation**: Already in `src/indicators/alligator.py`
  - `check_lips_touch_teeth_down()` for LONG exits
  - `check_lips_touch_teeth_up()` for SHORT exits

**Secondary Exits**:
1. **Stop Loss Hit**: Price hits trailing stop (red line) — close reason: `HARD_STOP`
2. **Take Profit Hit**: Price hits trailing TP (green line) — close reason: `ALLIGATOR_TP`
3. **Peak Giveback** (optional): bar-close retraced 35% of max favorable move — close reason: `PEAK_GIVEBACK_EXIT`

### Exit Priority Order

```
HARD_STOP  →  PEAK_GIVEBACK_EXIT  →  TRAIL_STOP  →  ALLIGATOR_TP
```

### Understanding PEAK_GIVEBACK_EXIT

`PEAK_GIVEBACK_EXIT` is **not a fixed take-profit**.  It is a bar-close retracement guard:

1. After entry, the tracker records the most favorable extreme price seen across all bars
   (highest bar-high for longs; lowest bar-low for shorts).
2. If the bar-close price retraces `PEAK_GIVEBACK_FRACTION` (default 35%) of the total
   favorable move back toward entry, the exit fires on that candle's close.
3. Exits are evaluated **only on bar close** — not intra-bar touch.

**Why it can close at a loss** (this is expected behaviour):

| Entry | Peak (MFE) | Fraction | Trigger Level | Bar Close | Result |
|-------|-----------|----------|---------------|-----------|--------|
| 100   | 101       | 35%      | 100.65        | 100.60    | −$0.40/unit loss |
| 100   | 110       | 35%      | 106.50        | 106.45    | +$6.45/unit gain |

With a small MFE (1 point) and a 35% retrace fraction, the trigger level sits just
0.65 points above entry.  A bar that closes below that level — and possibly below
entry — will trigger the exit even though it is a losing close.  This is by design;
no break-even floor exists in the current implementation.

To reduce negative `PEAK_GIVEBACK_EXIT` closes, a future phase can add a
minimum-MFE activation threshold (e.g. trigger only fires after 0.5% favorable
move) via `PEAK_GIVEBACK_MIN_MFE_PCT`.

## IBKR Order Execution

### Order Structure

When a signal is generated, the following orders are placed with IBKR:

1. **Main Order**: Market order to enter position
2. **Stop Loss Order**: Stop order at initial 2% level
3. **Take Profit Order**: Limit order at initial lips price

### Order Updates

As the trade progresses, orders are updated automatically:

**Every Candle Close**:
1. Calculate new teeth price (red line)
2. Calculate new lips price (green line)
3. Update stop loss if teeth moved favorably
4. Update take profit if lips moved favorably (and TP is activated)
5. Send updated orders to IBKR via `modify_position_sltp()`

**Exit Checks**:
1. Check if lips touched teeth → Close position
2. Check if stop loss hit → Position already closed by IBKR
3. Check if take profit hit → Position already closed by IBKR

### IBKR API Methods

**Place Order** (`IBKRAdapter.place_order()`):
```python
place_order(
    signal_type="BUY",
    symbol="AAPL",
    volume=100,
    expected_entry=175.25,
    stop_loss=171.75,  # 2% below entry
    take_profit=176.50,  # Initial lips price
    trade_id="uuid"
)
```

**Update Stop Loss and Take Profit** (`IBKRAdapter.modify_position_sltp()`):
```python
modify_position_sltp(
    trade_id="uuid",
    new_sl=172.50,  # Updated trailing stop (teeth)
    new_tp=177.80   # Updated trailing TP (lips)
)
```

**Close Position** (`IBKRAdapter.close_order()`):
```python
close_order(trade_id="uuid")
```

## Timeframe Configuration

### Supported Timeframes (Larger Timeframes Only)

✅ **15m** - 15 minutes
✅ **30m** - 30 minutes  
✅ **1h** - 1 hour (recommended primary)
✅ **2h** - 2 hours
✅ **4h** - 4 hours

❌ **NOT USED**: 1m, 3m, 5m (too short-term, too much API load)

### Primary Timeframe

**Recommended**: **1h** (1 hour)
- Good balance between trade frequency and quality
- Sufficient time for indicators to develop
- Manageable API load
- Better risk/reward ratios

### Market Coverage Strategy

**Rotating Scanner Ensures All Markets Get Scanned**:

1. **Batch Size**: 30 symbols per batch
2. **Rotation Delay**: 10 seconds between batches
3. **Cycle Time**: 10 minutes for full rotation
4. **Coverage**: ~180 symbols per 10-minute cycle

**Example with 100 symbols**:
- Batch 1 (symbols 1-30): Scan at minute 0
- Batch 2 (symbols 31-60): Scan at minute 0:10
- Batch 3 (symbols 61-90): Scan at minute 0:20
- Batch 4 (symbols 91-100): Scan at minute 0:30
- **Full rotation complete in ~4 minutes**
- Cycle repeats every 10 minutes

**No Market Left Behind**:
- Every symbol gets scanned within the rotation cycle
- Priority symbols (positions, signals) scanned first
- Live subscriptions for active positions
- Historical data for everything else

## Configuration Files

### 1. Environment Variables (`.env`)

```bash
# IBKR Connection
IBKR_HOST=127.0.0.1
IBKR_PORT=7497              # Paper trading
IBKR_CLIENT_ID=1

# Risk Parameters
STOP_LOSS_PCT=0.02              # 2% initial stop loss

# Peak-giveback exit (formerly TRAILING_TP_ENABLED / TRAILING_TP_GIVEBACK)
# Canonical env var names:
PEAK_GIVEBACK_ENABLED=true      # Enable bar-close retracement exit
PEAK_GIVEBACK_FRACTION=0.35     # 35% giveback of max favorable move triggers exit
# Legacy names still accepted as fallback (deprecated; will be removed in a future release):
# TRAILING_TP_ENABLED=true
# TRAILING_TP_GIVEBACK=0.35

# IBKR Rotation Settings
IBKR_BATCH_SIZE=30
IBKR_ROTATION_DELAY_SECONDS=10
IBKR_ROTATION_CYCLE_MINUTES=10
IBKR_MAX_LIVE_SUBSCRIPTIONS=80
```

### 2. Timeframe Configuration (`config_ibkr_timeframes.py`)

```python
# Supported timeframes
IBKR_TIMEFRAMES = ["15m", "30m", "1h", "2h", "4h"]

# Primary timeframe for scanning
PRIMARY_TIMEFRAME = "1h"

# Rotation configuration
ROTATION_CONFIG = {
    "batch_size": 30,
    "rotation_delay_seconds": 10,
    "rotation_cycle_minutes": 10,
    "max_live_subscriptions": 80,
}
```

## Trade Flow Example

### LONG Trade Example (BUY AAPL)

**Entry**:
- Signal generated at $175.00
- Alligator: Lips above teeth and jaw
- Vortex: VI+ crosses above VI-
- Stochastic: Enters above 80

**Orders Placed**:
1. Market BUY 100 shares @ $175.00
2. Stop Loss @ $171.50 (2% below, tracks teeth)
3. Take Profit @ $176.50 (initial lips price)

**Candle 1** (1 hour later):
- Price: $176.50
- Teeth (red): $172.00
- Lips (green): $177.00
- **Update**: Stop → $172.00, TP → $177.00

**Candle 2** (2 hours later):
- Price: $178.00
- Teeth (red): $173.00
- Lips (green): $178.50
- **Update**: Stop → $173.00, TP → $178.50 (TP now activated, 1% profit reached)

**Candle 3** (3 hours later):
- Price: $177.50
- Teeth (red): $173.50
- Lips (green): $178.00
- **Update**: Stop → $173.50, TP → $178.50 (TP doesn't move down)

**Exit** (4 hours later):
- Price: $177.00
- Lips touches teeth (green touches red)
- **Action**: Close position via `close_order()`
- **Result**: +$2.00/share profit (+1.14%)

## Key Features

✅ **Stop Loss**: 2% initial, trails red line (teeth)
✅ **Take Profit**: Trails green line (lips) as momentum continues
✅ **Auto Exit**: When green line touches red line
✅ **Larger Timeframes**: 15m, 30m, 1h, 2h, 4h only
✅ **Full Market Coverage**: All symbols scanned via rotation
✅ **IBKR Integration**: Orders placed and updated automatically
✅ **Paper Trading**: Safe testing environment

## Testing Checklist

Before going live:

- [ ] Test with small watchlist (5-10 symbols)
- [ ] Verify stop loss placement (2% from entry)
- [ ] Verify take profit placement (at lips price)
- [ ] Confirm stop loss trails teeth (red line)
- [ ] Confirm take profit trails lips (green line)
- [ ] Test exit when lips touches teeth
- [ ] Verify IBKR order updates work
- [ ] Monitor API rate limits
- [ ] Check all markets get scanned
- [ ] Review trade candidate logs

## Files Modified/Created

### Modified:
- `src/execution/ibkr_adapter.py` - Enhanced with SL/TP order management
- `src/scanner/market_scanner.py` - Integrated Alligator trailing TP
- `src/data/market_data.py` - Added IBKR data source
- `src/data/symbol_mapper.py` - Added IBKR support functions

### Created:
- `src/risk/alligator_trailing_tp.py` - Alligator trailing take profit
- `src/data/ibkr/position_coordinator.py` - Position-triggered subscriptions
- `src/notifications/trade_candidate_logger.py` - Comprehensive logging
- `config_ibkr_timeframes.py` - Timeframe configuration
- `test_ibkr_small_watchlist.py` - Small watchlist test script
- `validate_ibkr_signals.py` - Signal validation script
- `TRADE_EXECUTION_SUMMARY.md` - This document

## Next Steps

1. **Review Configuration**
   - Check `.env` settings
   - Review `config_ibkr_timeframes.py`
   - Adjust batch size if needed

2. **Test with Small Watchlist**
   ```bash
   python test_ibkr_small_watchlist.py
   ```

3. **Validate Signals**
   ```bash
   python validate_ibkr_signals.py
   ```

4. **Monitor First Trades**
   - Watch stop loss updates
   - Watch take profit updates
   - Verify exit conditions
   - Check logs/trade_candidates.log

5. **Scale Gradually**
   - Start with 10 symbols
   - Increase to 50 symbols
   - Monitor API usage
   - Scale to full universe

## Support

For issues:
1. Check `logs/trade_candidates.log` for detailed trade info
2. Check `logs/signals.log` for signal generation
3. Check `logs/trades.log` for trade execution
4. Review IBKR TWS/Gateway logs
5. Monitor IBKR API rate limits

## Summary

Your trading system now has:
- ✅ 2% initial stop loss trailing the red line (teeth)
- ✅ Take profit trailing the green line (lips)
- ✅ Auto-exit when green touches red
- ✅ Larger timeframes only (15m, 30m, 1h, 2h, 4h)
- ✅ Full market coverage via rotation
- ✅ Proper IBKR order execution with SL/TP
- ✅ Comprehensive logging for analysis

All parameters are attached to IBKR orders and updated automatically as the Alligator lines move.
