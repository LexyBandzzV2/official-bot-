"""FXCM Adapter — placeholder execution adapter.

FXCM execution is not implemented yet; this file exists so routing and config
can be completed safely with plug-in keys later.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.config import FXCM_API_KEY, FXCM_ACCESS_TOKEN

log = logging.getLogger(__name__)


class FXCMAdapter:
    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> bool:
        if not FXCM_API_KEY or not FXCM_ACCESS_TOKEN:
            log.warning("FXCM credentials missing — FXCM disabled")
            self._connected = False
            return False
        # TODO: implement FXCM connection when you add the library/REST client.
        log.warning("FXCM connect not implemented yet — placeholder only")
        self._connected = False
        return False

    def disconnect(self) -> None:
        self._connected = False

    def place_order(self, signal_type: str, symbol: str, volume: float, expected_entry: float, stop_loss: float, trade_id: str, take_profit: Optional[float] = None) -> Optional[dict]:
        if not self._connected:
            raise RuntimeError("FXCMAdapter not connected")
        log.warning("FXCM place_order not implemented yet (trade_id=%s)", trade_id)
        return None

    def close_order(self, trade_id: str) -> bool:
        log.warning("FXCM close_order not implemented yet (trade_id=%s)", trade_id)
        return False

    def update_trailing_stop(self, trade_id: str, new_sl: float) -> bool:
        log.warning("FXCM update_trailing_stop not implemented yet (trade_id=%s)", trade_id)
        return False

    def modify_position_sltp(self, trade_id: str, new_sl: float, new_tp: Optional[float] = None) -> bool:
        log.warning("FXCM modify_position_sltp not implemented yet (trade_id=%s)", trade_id)
        return False

