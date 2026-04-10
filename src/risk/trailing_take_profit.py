"""Peak Giveback exit — emits close reason PEAK_GIVEBACK_EXIT.

Semantics
---------
This is NOT a fixed take-profit.  It is a favorable-excursion guard:

  1. After entry the tracker records the extreme favorable price (highest     high for longs, lowest low for shorts) seen across all bar updates.
  2. Once a meaningful favorable move exists (peak beyond entry), if the
     bar-close price retraces ``giveback_frac`` of that max favorable move
     back toward entry, the exit fires.
  3. Exits are ONLY evaluated on bar close (not intra-bar).  A candle whose
     high posted a tiny new peak may still close below entry, resulting in a
     losing exit — this is expected and intentional behaviour.

Example (long):
    entry 100, peak 101, giveback_frac 0.35
    trigger_level = 101 - 0.35 × (101 - 100) = 100.65
    bar closes at 100.60 → PEAK_GIVEBACK_EXIT fires at a loss

Why losses are allowed
-----------------------
No break-even floor is enforced by this class.  Preventing negative
PEAK_GIVEBACK_EXIT closes requires either:
  • a minimum MFE activation threshold before giveback can fire, or
  • a break-even lock that moves trigger_level to entry once MFE ≥ N%.
Both are Phase 3 improvements; this class deliberately stays simple.
"""

from __future__ import annotations


class PeakGiveback:
    """Tracks bar-high/low peak; ``is_triggered(close)`` fires PEAK_GIVEBACK_EXIT.

    The emitted close reason is the string ``"PEAK_GIVEBACK_EXIT"`` — callers
    (RiskManager.check_exit_conditions) own that string; this class only
    reports whether the condition is met.
    """

    def __init__(
        self,
        direction: str,
        entry_price: float,
        giveback_frac: float,
        min_mfe_pct: float = 0.001,
    ) -> None:
        self.direction = direction.lower()
        if self.direction not in ("buy", "sell"):
            raise ValueError("direction must be 'buy' or 'sell'")
        self.entry_price = float(entry_price)
        self.giveback_frac = float(giveback_frac)
        self.peak_price = float(entry_price)
        self.min_mfe_pct = float(min_mfe_pct)
        # Pre-compute the minimum price the peak must reach before giveback can fire
        if self.direction == "buy":
            self._min_mfe_price = self.entry_price * (1.0 + self.min_mfe_pct)
        else:
            self._min_mfe_price = self.entry_price * (1.0 - self.min_mfe_pct)

    def update_bar(self, high: float, low: float) -> None:
        """Incorporate this bar's range into the running peak."""
        if self.direction == "buy":
            self.peak_price = max(self.peak_price, float(high))
        else:
            self.peak_price = min(self.peak_price, float(low))

    def trigger_level(self) -> float:
        """Price level at which giveback exit fires (for logging / broker)."""
        if self.direction == "buy":
            return self.peak_price - self.giveback_frac * (self.peak_price - self.entry_price)
        return self.peak_price + self.giveback_frac * (self.entry_price - self.peak_price)

    def is_triggered(self, close_price: float) -> bool:
        """True when close breaches the giveback line (only after meaningful favorable move)."""
        if self.direction == "buy":
            # Require peak to exceed entry by at least min_mfe_pct before giveback fires
            if self.peak_price < self._min_mfe_price:
                return False
            return float(close_price) <= self.trigger_level()
        # Sell (short): peak must fall below min_mfe_price before giveback fires
        if self.peak_price > self._min_mfe_price:
            return False
        return float(close_price) >= self.trigger_level()
