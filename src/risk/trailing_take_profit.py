"""Peak Giveback exit — emits close reason PEAK_GIVEBACK_EXIT.

Semantics
---------
This is NOT a fixed take-profit.  It is a favorable-excursion guard:

  1. After entry the tracker records the extreme favorable price (highest
     high for longs, lowest low for shorts) seen across all bar updates.
  2. Once the favorable move exceeds ``min_mfe_pct`` of the entry price,
     if the bar-close price retraces ``giveback_frac`` of that max favorable
     move back toward entry, the exit fires.
  3. Exits are ONLY evaluated on bar close (not intra-bar).

Example (long):
    entry 100, peak 101, giveback_frac 0.35, min_mfe_pct 0.005
    MFE = (101 - 100) / 100 = 1.0% ≥ 0.5% → guard cleared
    trigger_level = 101 - 0.35 × (101 - 100) = 100.65
    bar closes at 100.60 → PEAK_GIVEBACK_EXIT fires

Why min_mfe_pct matters
------------------------
Without a minimum MFE threshold, a candle that barely nudges past entry
(e.g. +0.05%) can trigger the giveback exit below entry, producing a loss
that is labelled a "strategic exit".  The guard prevents this by requiring
the peak to reach a meaningful favorable distance first.

The default minimum is 0.5% (configured via PEAK_GIVEBACK_MIN_MFE_PCT in .env).
"""

from __future__ import annotations


class PeakGiveback:
    """Tracks bar-high/low peak; ``is_triggered(close)`` fires PEAK_GIVEBACK_EXIT.

    The emitted close reason is the string ``"PEAK_GIVEBACK_EXIT"`` — callers
    (RiskManager.check_exit_conditions) own that string; this class only
    reports whether the condition is met.

    Parameters
    ----------
    direction:     ``"buy"`` or ``"sell"``.
    entry_price:   Price at which the trade was entered.
    giveback_frac: Fraction of the max favorable excursion that may retrace
                   before the exit fires (e.g. 0.35 = 35%).
    min_mfe_pct:   Minimum favorable excursion as a fraction of entry price
                   required before the exit is eligible (e.g. 0.005 = 0.5%).
                   Prevents closing at a loss when the move was tiny.
    """

    def __init__(
        self,
        direction:     str,
        entry_price:   float,
        giveback_frac: float,
        min_mfe_pct:   float = 0.005,
    ) -> None:
        self.direction = direction.lower()
        if self.direction not in ("buy", "sell"):
            raise ValueError("direction must be 'buy' or 'sell'")
        self.entry_price   = float(entry_price)
        self.giveback_frac = float(giveback_frac)
        self.min_mfe_pct   = float(min_mfe_pct)
        self.peak_price    = float(entry_price)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _mfe_fraction(self) -> float:
        """Current max favorable excursion as a fraction of entry price."""
        if self.direction == "buy":
            return (self.peak_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.peak_price) / self.entry_price

    def _min_mfe_cleared(self) -> bool:
        """Return True once the peak has moved enough to arm the exit."""
        return self._mfe_fraction() >= self.min_mfe_pct

    # ── Public API ────────────────────────────────────────────────────────────

    def update_bar(self, high: float, low: float) -> None:
        """Incorporate this bar's range into the running peak."""
        if self.direction == "buy":
            self.peak_price = max(self.peak_price, float(high))
        else:
            self.peak_price = min(self.peak_price, float(low))

    def trigger_level(self) -> float:
        """Price level at which the giveback exit fires (for logging / broker SL)."""
        if self.direction == "buy":
            return self.peak_price - self.giveback_frac * (self.peak_price - self.entry_price)
        return self.peak_price + self.giveback_frac * (self.entry_price - self.peak_price)

    def is_triggered(self, close_price: float) -> bool:
        """True when the bar close breaches the giveback line AND min MFE is met."""
        if self.direction == "buy":
            if self.peak_price <= self.entry_price:
                return False
            if not self._min_mfe_cleared():
                return False
            return float(close_price) <= self.trigger_level()
        # sell
        if self.peak_price >= self.entry_price:
            return False
        if not self._min_mfe_cleared():
            return False
        return float(close_price) >= self.trigger_level()
