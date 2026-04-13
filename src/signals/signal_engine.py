"""Signal Engine — orchestrates BuySignalWorker and SellSignalWorker.

Critical safety rule:
    A BUY signal and a SELL signal CANNOT both be valid on the same asset
    at the same time.  If both somehow fire simultaneously, BOTH are suppressed
    and the conflict is logged.

All candles must already be converted to Heikin Ashi before calling evaluate().
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

from src.data.heikin_ashi import convert_to_heikin_ashi
from src.signals.buy_worker  import BuySignalWorker
from src.signals.sell_worker import SellSignalWorker
from src.signals.types import BuySignalResult, SellSignalResult

logger = logging.getLogger(__name__)


class SignalEngine:
    """Runs both signal workers for one asset / timeframe pair.

    Usage
    -----
    engine = SignalEngine("EURUSD", "1h")
    buy, sell = engine.evaluate(raw_ohlcv_df)   # raw candles — engine converts HA internally
    """

    def __init__(self, asset: str, timeframe: str) -> None:
        self.asset      = asset
        self.timeframe  = timeframe
        self._buy_worker  = BuySignalWorker(asset, timeframe)
        self._sell_worker = SellSignalWorker(asset, timeframe)

    # ── Core evaluation ───────────────────────────────────────────────────────

    def evaluate(
        self,
        raw_df: pd.DataFrame,
    ) -> dict:
        """Evaluate buy and sell signals on raw OHLCV data.

        The engine converts candles to Heikin Ashi internally so callers
        don't have to remember.  Both workers receive the same HA DataFrame.

        Parameters
        ----------
        raw_df : Standard OHLCV DataFrame (open/high/low/close columns).

        Returns
        -------
        dict with keys {"buy": BuySignalResult, "sell": SellSignalResult, "conflict": bool}
        """
        ha_df = convert_to_heikin_ashi(raw_df)
        return self.evaluate_ha(ha_df)

    # ── Convenience ──────────────────────────────────────────────────────────

    def evaluate_ha(
        self,
        ha_df: pd.DataFrame,
    ) -> dict:
        """Same as evaluate() but accepts an already-converted HA DataFrame.

        Use this in the backtest engine where HA conversion is done once
        upfront for efficiency.
        """
        buy_result  = self._buy_worker.evaluate(ha_df)
        sell_result = self._sell_worker.evaluate(ha_df)

        if buy_result.is_valid and sell_result.is_valid:
            # Conflict: both fired in the window — keep the most recent one.
            # Compare the last completion bar index; higher index = more recent.
            buy_last  = buy_result.last_completion_bar  if hasattr(buy_result,  "last_completion_bar") else 0
            sell_last = sell_result.last_completion_bar if hasattr(sell_result, "last_completion_bar") else 0
            if sell_last > buy_last:
                buy_result.is_valid = False
                buy_result.rejection_reason = "CONFLICT_SUPPRESSED_SELL_NEWER"
                logger.debug("CONFLICT on %s %s — sell newer (%d > %d), keeping SELL",
                             self.asset, self.timeframe, sell_last, buy_last)
            else:
                sell_result.is_valid = False
                sell_result.rejection_reason = "CONFLICT_SUPPRESSED_BUY_NEWER"
                logger.debug("CONFLICT on %s %s — buy newer (%d >= %d), keeping BUY",
                             self.asset, self.timeframe, buy_last, sell_last)
            return {"buy": buy_result, "sell": sell_result, "conflict": True}

        return {"buy": buy_result, "sell": sell_result, "conflict": False}
