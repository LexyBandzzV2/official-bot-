"""Trailing Stop — two-layer ratcheting stop that only moves in your favour.

Layer 1 — Hard floor (never removed):
    BUY  → entry_price × 0.98  (2 % below entry)
    SELL → entry_price × 1.02  (2 % above entry)

Layer 2 — Alligator Teeth trail (ratchets in your favour):
    BUY  → as Teeth line rises, stop rises to track it (never pulls back down)
    SELL → as Teeth line falls, stop falls to track it (never pulls back up)

The current_stop can only improve (get closer to locking in profit).
It NEVER moves against the trade.

On each new candle:
    1. Get the current Teeth line price.
    2. For BUY:  candidate = max(hard_floor, teeth_price)
                 if candidate > current_stop: update
    3. For SELL: candidate = min(hard_floor, teeth_price)
                 if candidate < current_stop: update
"""

from __future__ import annotations


class TrailingStop:
    """Manages the dual-layer trailing stop for one open position."""

    def __init__(
        self,
        direction:    str,    # 'buy' or 'sell'
        entry_price:  float,
        initial_teeth:float,
        stop_loss_pct:float = 0.02,   # 2 %
    ) -> None:
        self.direction   = direction.lower()
        self.entry_price = entry_price

        # ── Layer 1: hard floor ───────────────────────────────────────────────
        if self.direction == "buy":
            self.hard_floor = round(entry_price * (1.0 - stop_loss_pct), 6)
        else:
            self.hard_floor = round(entry_price * (1.0 + stop_loss_pct), 6)

        # ── Start the trailing stop at the hard floor ─────────────────────────
        self.current_stop   = self.hard_floor
        self.max_trail      = self.hard_floor   # tracks the best level ever reached

        # Apply the initial teeth level immediately
        self.update(initial_teeth)

    # ── Core update ───────────────────────────────────────────────────────────

    def update(self, teeth_price: float) -> float:
        """Ratchet the trailing stop based on the current Teeth (red) line.

        The stop only moves in the direction that locks in more profit.

        Returns
        -------
        float : The updated current_stop level.
        """
        if self.direction == "buy":
            # Stop moves UP only (higher = more protected)
            candidate = max(self.hard_floor, teeth_price)
            if candidate > self.current_stop:
                self.current_stop = candidate
                self.max_trail    = candidate
        else:
            # Stop moves DOWN only (lower = more protected for a short)
            candidate = min(self.hard_floor, teeth_price)
            if candidate < self.current_stop:
                self.current_stop = candidate
                self.max_trail    = candidate

        return self.current_stop

    # ── Trigger check ─────────────────────────────────────────────────────────

    def is_triggered(self, current_price: float) -> bool:
        """Return True if price has touched or breached the trailing stop."""
        if self.direction == "buy":
            return current_price <= self.current_stop
        return current_price >= self.current_stop

    # ── Metrics ──────────────────────────────────────────────────────────────

    def locked_profit_pct(self) -> float:
        """Percentage of profit locked in by the trail (negative = loss floor)."""
        if self.direction == "buy":
            return (self.current_stop - self.entry_price) / self.entry_price * 100.0
        return (self.entry_price - self.current_stop) / self.entry_price * 100.0

    def locked_profit_usd(self, position_size: float) -> float:
        """Dollar amount of profit locked in (negative = loss still possible)."""
        if self.direction == "buy":
            return (self.current_stop - self.entry_price) * position_size
        return (self.entry_price - self.current_stop) * position_size

    def __repr__(self) -> str:
        return (
            f"TrailingStop({self.direction.upper()} | entry={self.entry_price:.5f} "
            f"| hard_floor={self.hard_floor:.5f} | current={self.current_stop:.5f} "
            f"| locked={self.locked_profit_pct():.2f}%)"
        )
