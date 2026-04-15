"""Trailing Stop — profit-locking ratchet that only moves in your favour.

Rules (user spec):

Layer 1 — Hard floor (never removed):
    BUY  → entry_price × 0.98   (2 % below entry)
    SELL → entry_price × 1.02   (2 % above entry)

Layer 2 — Profit-based lock (activates at +1 % profit):
    Once the position is up ≥ 1 %, the stop moves to lock in
    50 % of the current profit:

        locked_pct = current_profit_pct × 0.5
        new_stop   = entry_price ± locked_pct

    Examples (BUY):
        profit 1.00 % → stop at entry + 0.50 %
        profit 1.50 % → stop at entry + 0.75 %
        profit 2.00 % → stop at entry + 1.00 %
        profit 3.00 % → stop at entry + 1.50 %
        profit 5.00 % → stop at entry + 2.50 %

    The stop NEVER moves against the trade.  Each update only
    tightens the stop further toward the current price.

Trailing Take Profit — only moves on high-momentum candles:
    A momentum candle is one whose body ≥ ATR × momentum_multiplier
    and moves in the trade's favour.  Only then does the TP extend
    further by (atr × tp_extension_atr).  Otherwise the TP stays put.
"""

from __future__ import annotations

from typing import Optional


class TrailingStop:
    """Profit-locking trailing stop for one open position."""

    def __init__(
        self,
        direction:               str,      # 'buy' or 'sell'
        entry_price:             float,
        initial_teeth:           float = 0.0,   # kept for backward compatibility
        stop_loss_pct:           float = 0.02,  # 2 % hard floor
        activation_profit_pct:   float = 1.0,   # trail activates at +1 % profit
        lock_ratio:              float = 0.5,   # lock 50 % of current profit
    ) -> None:
        self.direction              = direction.lower()
        self.entry_price            = float(entry_price)
        self.stop_loss_pct          = stop_loss_pct
        self.activation_profit_pct  = activation_profit_pct
        self.lock_ratio             = lock_ratio

        # Layer 1: hard floor (always present, never removed)
        if self.direction == "buy":
            self.hard_floor = round(self.entry_price * (1.0 - stop_loss_pct), 8)
        else:
            self.hard_floor = round(self.entry_price * (1.0 + stop_loss_pct), 8)

        # Stop starts at the hard floor and only ratchets in the favour direction.
        self.current_stop = self.hard_floor
        self.max_trail    = self.hard_floor
        self._activated   = False   # becomes True once profit >= activation_profit_pct

    # ── Core update ───────────────────────────────────────────────────────────

    def update(
        self,
        teeth_price:  float = 0.0,
        current_price: Optional[float] = None,
    ) -> float:
        """Ratchet the trailing stop based on the CURRENT profit percentage.

        `teeth_price` is accepted for backward compatibility but ignored —
        the new logic is purely profit-based.

        Returns the (possibly updated) current_stop level.
        """
        if current_price is None or current_price <= 0:
            return self.current_stop

        # Compute unrealised profit %
        if self.direction == "buy":
            profit_pct = (current_price - self.entry_price) / self.entry_price * 100.0
        else:
            profit_pct = (self.entry_price - current_price) / self.entry_price * 100.0

        # Trail is gated behind the activation threshold (+1 %)
        if profit_pct < self.activation_profit_pct:
            return self.current_stop

        self._activated = True

        # Escalating lock ratio — gets more aggressive as profit grows.
        # The closer to the 5% target, the tighter the stop.
        #
        #   Profit   Lock ratio   Locked SL (BUY example)
        #   +1.00%      50%       entry + 0.50%
        #   +2.00%      60%       entry + 1.20%
        #   +3.00%      70%       entry + 2.10%
        #   +4.00%      75%       entry + 3.00%
        #   +5.00%      80%       entry + 4.00%
        #   +6.00%      83%       entry + 5.00%
        #   +8.00%      85%       entry + 6.80%
        if profit_pct < 2.0:
            _lock_ratio = 0.50
        elif profit_pct < 3.0:
            _lock_ratio = 0.60
        elif profit_pct < 4.0:
            _lock_ratio = 0.70
        elif profit_pct < 5.0:
            _lock_ratio = 0.75
        else:
            # ≥ 5%: ramp from 80% toward 85% cap as profit keeps climbing
            _lock_ratio = min(0.80 + (profit_pct - 5.0) * 0.01, 0.85)

        locked_pct = profit_pct * _lock_ratio
        if self.direction == "buy":
            candidate = self.entry_price * (1.0 + locked_pct / 100.0)
            candidate = round(candidate, 8)
            if candidate > self.current_stop:
                self.current_stop = candidate
                self.max_trail    = candidate
        else:
            candidate = self.entry_price * (1.0 - locked_pct / 100.0)
            candidate = round(candidate, 8)
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
        """% of profit currently locked in (negative = loss-floor only)."""
        if self.direction == "buy":
            return (self.current_stop - self.entry_price) / self.entry_price * 100.0
        return (self.entry_price - self.current_stop) / self.entry_price * 100.0

    def locked_profit_usd(self, position_size: float) -> float:
        if self.direction == "buy":
            return (self.current_stop - self.entry_price) * position_size
        return (self.entry_price - self.current_stop) * position_size

    def is_activated(self) -> bool:
        return self._activated

    def __repr__(self) -> str:
        return (
            f"TrailingStop({self.direction.upper()} | entry={self.entry_price:.5f} "
            f"| hard_floor={self.hard_floor:.5f} | current={self.current_stop:.5f} "
            f"| locked={self.locked_profit_pct():.2f}% "
            f"| activated={self._activated})"
        )


class TrailingTakeProfit:
    """Momentum-driven trailing take profit.

    TP only extends further when the most recent candle shows strong
    momentum in the trade's favour.  Otherwise the TP stays locked.
    """

    def __init__(
        self,
        direction:           str,       # 'buy' or 'sell'
        entry_price:         float,
        initial_tp:          float,
        momentum_multiplier: float = 1.0,   # candle body ≥ ATR × this → momentum
        tp_extension_atr:    float = 2.5,   # move TP by this × ATR when momentum hits
    ) -> None:
        self.direction           = direction.lower()
        self.entry_price         = float(entry_price)
        self.current_tp          = float(initial_tp)
        self.momentum_multiplier = momentum_multiplier
        self.tp_extension_atr    = tp_extension_atr

    def update(
        self,
        current_price: float,
        candle_body:   float,    # |close - open| of last candle
        candle_dir:    int,      # +1 = bullish candle, -1 = bearish, 0 = doji
        atr:           float,
    ) -> float:
        """Return new TP if momentum candle aligned with trade; else unchanged."""
        if atr <= 0 or current_price <= 0:
            return self.current_tp

        # Must be a high-momentum candle
        if candle_body < atr * self.momentum_multiplier:
            return self.current_tp

        # Must be in the trade's favour direction
        if self.direction == "buy":
            if candle_dir <= 0:
                return self.current_tp
            candidate = current_price + atr * self.tp_extension_atr
            if candidate > self.current_tp:
                self.current_tp = round(candidate, 8)
        else:
            if candle_dir >= 0:
                return self.current_tp
            candidate = current_price - atr * self.tp_extension_atr
            if candidate < self.current_tp and candidate > 0:
                self.current_tp = round(candidate, 8)

        return self.current_tp

    def is_triggered(self, current_price: float) -> bool:
        if self.direction == "buy":
            return current_price >= self.current_tp
        return current_price <= self.current_tp

    def __repr__(self) -> str:
        return (
            f"TrailingTakeProfit({self.direction.upper()} | entry={self.entry_price:.5f} "
            f"| current_tp={self.current_tp:.5f})"
        )
