"""Peak giveback — trailing take-profit style exit without a fixed price target.

Tracks the best favorable extreme since entry (long: highest high; short: lowest low).
If price retraces by ``giveback_frac`` of the max favorable move from entry, exit.

Example (long): entry 100, peak 110, giveback 0.35 → exit if close <= 110 - 0.35×10 = 106.5.
"""

from __future__ import annotations


class PeakGiveback:
    """Updates peak from bar high/low; ``is_triggered(close)`` for exit on giveback."""

    def __init__(
        self,
        direction: str,
        entry_price: float,
        giveback_frac: float,
    ) -> None:
        self.direction = direction.lower()
        if self.direction not in ("buy", "sell"):
            raise ValueError("direction must be 'buy' or 'sell'")
        self.entry_price = float(entry_price)
        self.giveback_frac = float(giveback_frac)
        self.peak_price = float(entry_price)

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
            if self.peak_price <= self.entry_price:
                return False
            return float(close_price) <= self.trigger_level()
        if self.peak_price >= self.entry_price:
            return False
        return float(close_price) >= self.trigger_level()
