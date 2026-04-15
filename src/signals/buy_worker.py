
"""BUY Signal Worker — detects BUY signals ONLY.

This worker has zero knowledge of sell signal logic.
Buy and sell are two completely separate systems.

All candles passed in MUST already be converted to Heikin Ashi.
The worker trusts the caller has done this conversion.

Signal rules:
    Point 1 — Alligator: lips above both teeth and jaw (signal on first bar where that
              is true; may take multiple bars to get there). No signal until both are crossed.
    Point 2 — Stochastic: first bar where K or D enters above 80
    Point 3 — Vortex: VI+ crosses above VI-
    Entry   : all 3 within POINT_COMPLETION_WINDOW bars (confluence.py)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.alligator  import calculate_alligator
from src.indicators.stochastic import calculate_stochastic
from src.indicators.vortex     import calculate_vortex
from src.signals.confluence    import analyze_buy
from src.signals.types import BuySignalResult
from src.signals.strategy_mode import timeframe_to_mode


class BuySignalWorker:
    """Evaluates buy signals.  Never shares logic with SellSignalWorker."""

    def __init__(self, asset: str, timeframe: str) -> None:
        self.asset     = asset
        self.timeframe = timeframe

    # ── Main evaluation ───────────────────────────────────────────────────────

    def evaluate(self, ha_df: pd.DataFrame) -> BuySignalResult:
        """Run all buy-signal checks against a Heikin Ashi candle DataFrame.

        Parameters
        ----------
        ha_df : DataFrame already converted to Heikin Ashi.
                Must have at minimum: ha_open, ha_high, ha_low, ha_close,
                ha_body, ha_range, is_doji, is_bullish, is_bearish.
        """
        if len(ha_df) < 30:
            return BuySignalResult(asset=self.asset, timeframe=self.timeframe,
                                   rejection_reason="INSUFFICIENT_DATA")

        # ── Indicators on chart OHLC (open/high/low/close); HA kept for display ─
        df = ha_df.copy()
        alligator_df = calculate_alligator(df)
        stoch_df     = calculate_stochastic(df)
        vortex_df    = calculate_vortex(df)

        df["jaw"]   = alligator_df["jaw"]
        df["teeth"] = alligator_df["teeth"]
        df["lips"]  = alligator_df["lips"]
        df["stoch_k"]          = stoch_df["stoch_k"]
        df["stoch_d"]          = stoch_df["stoch_d"]
        df["vi_plus"]          = vortex_df["vi_plus"]
        df["vi_minus"]         = vortex_df["vi_minus"]

        # ── 3-point confluence (rolling window) ───────────────────────────────
        ab = analyze_buy(df)
        completions = ab["completions"]
        window = 15  # match POINT_COMPLETION_WINDOW
        # Signal valid if all 3 points met within last 10 candles
        is_valid = ab["valid_last"]
        
        # Resolve proper datetime for last signal
        # If valid (all 3 fired in last window): find the most recent event bar
        # Otherwise: fall back to the last full completion bar in history
        last_signal_str = None

        def _bar_to_ts(df, idx):
            """Get a human-readable datetime string from a DataFrame bar index."""
            import pandas as pd
            if "timestamp" in df.columns:
                ts = df["timestamp"].iloc[idx]
            elif hasattr(df.index, "dtype") and str(df.index.dtype).startswith("datetime"):
                ts = df.index[idx]
            elif "time" in df.columns:
                ts = df["time"].iloc[idx]
            else:
                ts = df.index[idx]
            try:
                if isinstance(ts, (int, float)):
                    return None
                
                from src.config import TIMEZONE
                import pytz
                
                dt = pd.Timestamp(ts)
                if dt.tz is None:
                    dt = dt.tz_localize('UTC')
                dt = dt.tz_convert(TIMEZONE)
                return dt.strftime("%Y-%m-%d  %I:%M %p")
            except Exception:
                return str(ts)

        if is_valid:
            # Find the last bar in the recent window where any indicator fired
            n = len(df)
            start_idx = max(0, n - window)
            from src.indicators.alligator  import alligator_buy_event
            from src.indicators.stochastic import stochastic_buy_event
            from src.indicators.vortex     import vortex_buy_event
            last_event_idx = None
            for i in range(n - 1, start_idx - 1, -1):
                if i == 0:
                    continue
                prev, curr = df.iloc[i - 1], df.iloc[i]
                if alligator_buy_event(prev, curr) or stochastic_buy_event(prev, curr) or vortex_buy_event(prev, curr):
                    last_event_idx = i
                    break
            if last_event_idx is not None:
                last_signal_str = _bar_to_ts(df, last_event_idx)
        elif completions:
            last_signal_str = _bar_to_ts(df, completions[-1])

        points           = ab["points"]
        alligator_point  = ab["alligator_point"]
        stochastic_point = ab["stochastic_point"]
        vortex_point     = ab["vortex_point"]

        # ── Entry data from the last HA candle ────────────────────────────────
        last        = df.iloc[-1]
        entry_price = float(last.get("ha_close", last["close"]))
        stop_loss   = round(entry_price * 0.98, 6)  # 2 % below entry (hard floor)

        jaw_price   = float(last["jaw"])   if not np.isnan(last["jaw"])   else 0.0
        teeth_price = float(last["teeth"]) if not np.isnan(last["teeth"]) else 0.0
        lips_price  = float(last["lips"])  if not np.isnan(last["lips"])  else 0.0

        # Est. Move: realistic ATR-based target (1.5 × ATR-14 as % of entry).
        # The previous jaw-distance metric showed the entry→jaw gap which is
        # far below price in an uptrend — resulting in inflated, misleading numbers.
        # ATR-based gives the statistically expected single-candle move range.
        try:
            from src.indicators.utils import latest_atr
            _atr_val = latest_atr(df, period=14)
            profit_estimate_pct = (_atr_val / entry_price * 100 * 1.5) if _atr_val > 0 else 0.0
        except Exception:
            profit_estimate_pct = (
                abs(entry_price - jaw_price) / entry_price * 100
                if jaw_price > 0 else 0.0
            )

        # ── Notification message ──────────────────────────────────────────────
        notification_message = ""
        if is_valid:
            notification_message = (
                f"BUY SIGNAL — {self.asset} {self.timeframe}\n"
                f"Points: 3/3 confirmed (within last {window} bars)\n"
                f"Alligator: lips finished above teeth and jaw (long)\n"
                f"Stochastic: K or D entered above 80\n"
                f"Vortex: VI+ crossed above VI-\n"
                f"Entry:     ${entry_price:.5f}\n"
                f"Stop Loss: ${stop_loss:.5f} (5% below entry)\n"
                f"Est. move: {profit_estimate_pct:.2f}%\n"
                f"Exit when: lips touches teeth again (downward cross)"
            )

        return BuySignalResult(
            signal_type         = "BUY",
            is_valid            = is_valid,
            points              = points,
            max_points          = 3,
            signals_in_history  = ab["count"],
            alligator_point     = alligator_point,
            stochastic_point    = stochastic_point,
            vortex_point        = vortex_point,
            # staircase_confirmed removed
            entry_price         = entry_price,
            stop_loss           = stop_loss,
            stop_loss_pct       = 5.0,
            profit_estimate_pct = round(profit_estimate_pct, 2),
            take_profit_trigger = "lips_crosses_down_to_teeth",
            notification_message= notification_message,
            asset               = self.asset,
            timeframe           = self.timeframe,
            jaw_price           = jaw_price,
            teeth_price         = teeth_price,
            lips_price          = lips_price,
            last_signal_time    = last_signal_str,
            strategy_mode       = timeframe_to_mode(self.timeframe),
            # Phase 5: partial indicator fields set here; scanner appends ML/AI suffix
            indicator_flags     = "+".join(
                f for f, hit in [
                    ("alligator",  alligator_point),
                    ("stochastic", stochastic_point),
                    ("vortex",     vortex_point),
                ] if hit
            ) or None,
            entry_reason_code   = "+".join(
                a for a, hit in [
                    ("al", alligator_point),
                    ("st", stochastic_point),
                    ("vo", vortex_point),
                ] if hit
            ) or None,
        )
