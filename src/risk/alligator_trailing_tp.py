"""Alligator Trailing Take Profit — trails the green line (lips) as momentum continues.

This module implements a take profit that follows the Alligator lips line,
locking in profits as the trend continues in your favor.

For LONG trades:
- Take profit starts at lips price when trade opens
- As lips rises, take profit rises to track it
- Take profit never moves down (only ratchets up)
- Exit when lips touches teeth (separate exit condition)

For SHORT trades:
- Take profit starts at lips price when trade opens
- As lips falls, take profit falls to track it
- Take profit never moves up (only ratchets down)
- Exit when lips touches teeth (separate exit condition)

The take profit provides a dynamic target that moves with momentum,
allowing winners to run while protecting against sudden reversals.
"""

from __future__ import annotations


class AlligatorTrailingTP:
    """Manages trailing take profit based on Alligator lips (green line)."""

    def __init__(
        self,
        direction: str,      # 'buy' or 'sell'
        entry_price: float,
        initial_lips: float,
        min_profit_pct: float = 0.005,  # Minimum 0.5% profit before TP activates (suits 1m/3m scalp timeframes)
    ) -> None:
        """Initialize trailing take profit.
        
        Args:
            direction: 'buy' or 'sell'
            entry_price: Entry price of the trade
            initial_lips: Initial lips (green line) price
            min_profit_pct: Minimum profit percentage before TP starts trailing
        """
        self.direction = direction.lower()
        self.entry_price = entry_price
        self.min_profit_pct = min_profit_pct
        
        # Calculate minimum profit threshold
        if self.direction == "buy":
            self.min_profit_price = entry_price * (1.0 + min_profit_pct)
        else:
            self.min_profit_price = entry_price * (1.0 - min_profit_pct)
        
        # Initialize take profit at lips price
        self.current_tp = initial_lips
        self.best_lips = initial_lips
        
        # Track if TP has been activated (min profit reached)
        self.activated = False

    def update(self, lips_price: float) -> float:
        """Update take profit based on current lips (green line) price.
        
        The TP only moves in the favorable direction (up for longs, down for shorts).
        TP only starts trailing after minimum profit threshold is reached.
        
        Args:
            lips_price: Current Alligator lips price
            
        Returns:
            Updated take profit price
        """
        if self.direction == "buy":
            # For longs: TP moves UP only
            # Check if we've reached minimum profit threshold
            if not self.activated and lips_price >= self.min_profit_price:
                self.activated = True
                log.info(f"Take profit activated at {lips_price:.4f} (min profit reached)")
            
            # Only trail if activated
            if self.activated:
                if lips_price > self.current_tp:
                    self.current_tp = lips_price
                    self.best_lips = lips_price
        else:
            # For shorts: TP moves DOWN only
            # Check if we've reached minimum profit threshold
            if not self.activated and lips_price <= self.min_profit_price:
                self.activated = True
                log.info(f"Take profit activated at {lips_price:.4f} (min profit reached)")
            
            # Only trail if activated
            if self.activated:
                if lips_price < self.current_tp:
                    self.current_tp = lips_price
                    self.best_lips = lips_price
        
        return self.current_tp

    def is_triggered(self, current_price: float) -> bool:
        """Check if current price has hit the take profit.
        
        Only triggers if TP has been activated (min profit reached).
        
        Args:
            current_price: Current market price
            
        Returns:
            True if take profit is hit
        """
        if not self.activated:
            return False
        
        if self.direction == "buy":
            return current_price >= self.current_tp
        return current_price <= self.current_tp

    def locked_profit_pct(self) -> float:
        """Calculate percentage profit locked in by current TP level.
        
        Returns:
            Profit percentage (negative if TP is below/above entry)
        """
        if self.direction == "buy":
            return (self.current_tp - self.entry_price) / self.entry_price * 100.0
        return (self.entry_price - self.current_tp) / self.entry_price * 100.0

    def locked_profit_usd(self, position_size: float) -> float:
        """Calculate dollar profit locked in by current TP level.
        
        Args:
            position_size: Position size in units
            
        Returns:
            Dollar profit amount
        """
        if self.direction == "buy":
            return (self.current_tp - self.entry_price) * position_size
        return (self.entry_price - self.current_tp) * position_size

    def __repr__(self) -> str:
        status = "ACTIVE" if self.activated else "WAITING"
        return (
            f"AlligatorTrailingTP({self.direction.upper()} | "
            f"entry={self.entry_price:.5f} | "
            f"current_tp={self.current_tp:.5f} | "
            f"status={status} | "
            f"locked={self.locked_profit_pct():.2f}%)"
        )


# Import logging at module level
import logging
log = logging.getLogger(__name__)
