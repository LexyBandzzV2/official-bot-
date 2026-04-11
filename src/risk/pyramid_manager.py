"""PyramidManager — tracks and controls scale-in entries for open trades.

A "pyramid" entry is a second order placed on the same asset as an already-open
trade, triggered when the trade has moved meaningfully in our favor and momentum
is still intact.  This lets us add leverage selectively to confirmed winners
rather than every entry.

Rules
-----
- Each trade may be pyramided at most MAX_PYRAMID_PER_TRADE times (default 1).
- A pyramid entry is only triggered when unrealized profit exceeds
  PYRAMID_TRIGGER_PCT (default 1.5%) — the trade must be in the green first.
- Once triggered, a pyramid entry uses PYRAMID_RISK_PCT (1.5%) of account
  balance with PYRAMID_LEVERAGE (3×) position sizing — giving 3× the dollar
  exposure of a standard entry for the same account-risk %.
- If the pyramid trigger fires but risk_manager.can_pyramid() rejects it
  (kill switch, position cap), the trigger is skipped silently.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class PyramidManager:
    """Tracks which trades have been pyramided and evaluates trigger conditions."""

    def __init__(self) -> None:
        # Maps trade_id → number of scale-ins already placed
        self._scale_in_count: dict[str, int] = {}

    # ── Query ─────────────────────────────────────────────────────────────────

    def scale_in_count(self, trade_id: str) -> int:
        """Return how many scale-in entries have been placed for this trade."""
        return self._scale_in_count.get(trade_id, 0)

    def already_pyramided(self, trade_id: str, max_per_trade: int = 1) -> bool:
        """True if this trade has already hit its pyramid limit."""
        return self._scale_in_count.get(trade_id, 0) >= max_per_trade

    # ── Trigger evaluation ────────────────────────────────────────────────────

    def should_trigger(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        trigger_pct: float,
    ) -> bool:
        """Return True when unrealized profit meets the pyramid trigger threshold.

        Parameters
        ----------
        direction:     "buy" or "sell"
        entry_price:   Price at which the base trade was opened.
        current_price: Latest market price.
        trigger_pct:   Minimum profit % required before scaling in (e.g. 1.5 = 1.5%).
        """
        if entry_price <= 0 or current_price <= 0:
            return False
        if direction == "buy":
            profit_pct = (current_price - entry_price) / entry_price * 100.0
        else:
            profit_pct = (entry_price - current_price) / entry_price * 100.0
        return profit_pct >= trigger_pct

    # ── State update ──────────────────────────────────────────────────────────

    def record_pyramid(self, trade_id: str) -> None:
        """Increment the scale-in counter for a trade after a successful pyramid entry."""
        self._scale_in_count[trade_id] = self._scale_in_count.get(trade_id, 0) + 1
        log.info("Pyramid recorded for trade %s (total scale-ins: %d)",
                 trade_id, self._scale_in_count[trade_id])

    def remove_trade(self, trade_id: str) -> None:
        """Clean up tracking state when a trade is closed."""
        self._scale_in_count.pop(trade_id, None)

    # ── Position sizing ───────────────────────────────────────────────────────

    @staticmethod
    def pyramid_position_size(
        account_balance: float,
        entry_price: float,
        stop_loss_price: float,
        risk_pct: float,
        leverage: float,   # kept as param for API compatibility, not used in calculation
    ) -> float:
        """Calculate base position size for the scale-in entry.

        Returns the number of units before leverage is applied.
        Pass the result directly to broker_router.place_order(volume=..., leverage=leverage).
        The router will apply the leverage multiplier to the volume automatically.
        """
        if account_balance <= 0 or entry_price <= 0:
            return 0.0
        price_risk = abs(entry_price - stop_loss_price)
        if price_risk == 0:
            return 0.0
        dollar_risk = account_balance * risk_pct
        base_units = dollar_risk / price_risk
        return round(base_units, 6)
