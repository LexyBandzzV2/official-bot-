
"""SELL Signal Worker — detects SELL signals ONLY.

This worker has zero knowledge of buy signal logic.
Buy and sell are two completely separate systems.

All candles passed in MUST already be converted to Heikin Ashi.

Signal rules (Pine-aligned):
    Point 1 — Alexis Alligator short entry (fractal breakout or crossunder(close,lips))
    Point 2 — Stochastic: first bar where K or D enters below 20
    Point 3 — Vortex: VI- crosses above VI+
    Entry   : all 3 within POINT_COMPLETION_WINDOW bars (confluence.py)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.alligator  import calculate_alligator
from src.indicators.stochastic import calculate_stochastic
from src.indicators.vortex     import calculate_vortex
from src.signals.confluence    import analyze_sell
from src.signals.types import SellSignalResult


class SellSignalWorker:
    """Evaluates sell signals.  Never shares logic with BuySignalWorker."""

    def __init__(self, asset: str, timeframe: str) -> None:
        self.asset     = asset
        self.timeframe = timeframe

    # ── Main evaluation ───────────────────────────────────────────────────────

    def evaluate(self, ha_df: pd.DataFrame) -> SellSignalResult:
        """Run all sell-signal checks against a Heikin Ashi candle DataFrame.

        Parameters
        ----------
        ha_df : DataFrame already converted to Heikin Ashi.
        """
        if len(ha_df) < 30:
            return SellSignalResult(asset=self.asset, timeframe=self.timeframe,
                                    rejection_reason="INSUFFICIENT_DATA")

        df = ha_df.copy()
        alligator_df = calculate_alligator(df)
        stoch_df     = calculate_stochastic(df)
        vortex_df    = calculate_vortex(df)

        df["jaw"]              = alligator_df["jaw"]
        df["teeth"]            = alligator_df["teeth"]
        df["lips"]             = alligator_df["lips"]
        df["last_up_fractal"]  = alligator_df["last_up_fractal"]
        df["last_down_fractal"]= alligator_df["last_down_fractal"]
        df["stoch_k"]          = stoch_df["stoch_k"]
        df["stoch_d"]          = stoch_df["stoch_d"]
        df["vi_plus"]          = vortex_df["vi_plus"]
        df["vi_minus"]         = vortex_df["vi_minus"]

        # ── 3-point confluence (rolling window) ───────────────────────────────
        sl = analyze_sell(df)
        is_valid         = sl["valid_last"]
        points           = sl["points"]
        alligator_point  = sl["alligator_point"]
        stochastic_point = sl["stochastic_point"]
        vortex_point     = sl["vortex_point"]

        # ── Entry data from the last HA candle ────────────────────────────────
        last        = df.iloc[-1]
        entry_price = float(last["ha_close"])
        stop_loss   = round(entry_price * 1.02, 6)  # 2 % ABOVE entry (short)

        jaw_price   = float(last["jaw"])   if not np.isnan(last["jaw"])   else 0.0
        teeth_price = float(last["teeth"]) if not np.isnan(last["teeth"]) else 0.0
        lips_price  = float(last["lips"])  if not np.isnan(last["lips"])  else 0.0

        profit_estimate_pct = (
            abs(jaw_price - entry_price) / entry_price * 100
            if jaw_price > 0 else 0.0
        )

        # ── Notification message ──────────────────────────────────────────────
        notification_message = ""
        if is_valid:
            notification_message = (
                f"SELL SIGNAL — {self.asset} {self.timeframe}\n"
                f"Points: 3/3 confirmed\n"
                f"Alligator: Alexis short entry (Pine)\n"
                f"Stochastic: K or D entered below 20\n"
                f"Vortex: VI- crossed above VI+\n"
                f"Entry:     ${entry_price:.5f}\n"
                f"Stop Loss: ${stop_loss:.5f} (2% above entry)\n"
                f"Est. move: {profit_estimate_pct:.2f}%\n"
                f"Exit when: lips touches teeth again (upward cross)"
            )

        return SellSignalResult(
            signal_type         = "SELL",
            is_valid            = is_valid,
            points              = points,
            max_points          = 3,
            signals_in_history  = sl["count"],
            alligator_point     = alligator_point,
            stochastic_point    = stochastic_point,
            vortex_point        = vortex_point,
            # staircase_confirmed removed
            entry_price         = entry_price,
            stop_loss           = stop_loss,
            stop_loss_pct       = 2.0,
            profit_estimate_pct = round(profit_estimate_pct, 2),
            take_profit_trigger = "lips_crosses_up_to_teeth",
            notification_message= notification_message,
            asset               = self.asset,
            timeframe           = self.timeframe,
            jaw_price           = jaw_price,
            teeth_price         = teeth_price,
            lips_price          = lips_price,
        )
