"""Cross-Timeframe Signal Confirmation (replaces single-bar MTF check).

Instead of checking alligator direction on a higher TF, this system tracks
when full signals (3/3 confluence) fire across timeframes. A signal is
CONFIRMED when two adjacent timeframes both show the same direction within
a 25-candle window of each other.

Adjacent timeframe pairs (either order counts):
  1m <-> 2m <-> 3m <-> 5m <-> 15m <-> 1h <-> 2h <-> 4h
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Adjacent timeframe map ────────────────────────────────────────────────────
_ADJACENT_TFS: dict[str, list[str]] = {
    "1m":  ["2m", "3m"],
    "2m":  ["1m", "3m"],
    "3m":  ["2m", "5m"],
    "5m":  ["3m", "15m"],
    "15m": ["5m", "1h"],
    "1h":  ["15m", "4h"],
    "2h":  ["1h", "4h"],
    "4h":  ["1h", "2h"],
}

# ── Minutes per candle ────────────────────────────────────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m":  1,
    "2m":  2,
    "3m":  3,
    "5m":  5,
    "15m": 15,
    "30m": 30,
    "1h":  60,
    "2h":  120,
    "4h":  240,
    "1d":  1440,
}

_WINDOW_CANDLES = 50


# ── Signal memory store ───────────────────────────────────────────────────────

class SignalMemory:
    """Thread-safe store for pending cross-TF signals.

    Keyed by (symbol, timeframe, direction) -> datetime of the signal.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (symbol, timeframe, direction) -> datetime (UTC)
        self._store: dict[tuple[str, str, str], datetime] = {}

    def record(self, symbol: str, timeframe: str, direction: str, timestamp: datetime) -> None:
        """Store or refresh a signal entry."""
        key = (symbol, timeframe, direction)
        with self._lock:
            self._store[key] = timestamp

    def is_confirmed(
        self,
        symbol: str,
        timeframe: str,
        direction: str,
        timestamp: datetime,
    ) -> bool:
        """Return True if any adjacent TF fired the same direction within 25 candles.

        The window size is 25 candles measured in the BASE timeframe's minutes,
        converted to seconds.
        """
        tf_mins = _TF_MINUTES.get(timeframe, 1)
        window_seconds = _WINDOW_CANDLES * tf_mins * 60

        adjacent = _ADJACENT_TFS.get(timeframe, [])
        with self._lock:
            for adj_tf in adjacent:
                key = (symbol, adj_tf, direction)
                adj_ts = self._store.get(key)
                if adj_ts is None:
                    continue
                age_seconds = abs((timestamp - adj_ts).total_seconds())
                if age_seconds <= window_seconds:
                    log.debug(
                        "MTF confirmed: %s %s %s — adjacent %s fired %.0fs ago (window %ds)",
                        symbol, timeframe, direction, adj_tf, age_seconds, window_seconds,
                    )
                    return True
        return False

    def cleanup(self) -> None:
        """Remove entries older than 25 candles on their own timeframe."""
        now = datetime.now(tz=timezone.utc)
        expired_keys = []
        with self._lock:
            for (symbol, timeframe, direction), ts in self._store.items():
                tf_mins = _TF_MINUTES.get(timeframe, 1)
                window_seconds = _WINDOW_CANDLES * tf_mins * 60
                # Ensure ts is timezone-aware for comparison
                if ts.tzinfo is None:
                    ts_aware = ts.replace(tzinfo=timezone.utc)
                else:
                    ts_aware = ts
                age = (now - ts_aware).total_seconds()
                if age > window_seconds:
                    expired_keys.append((symbol, timeframe, direction))
            for key in expired_keys:
                del self._store[key]
        if expired_keys:
            log.debug("MTF cleanup: removed %d expired entries", len(expired_keys))


# ── Module-level singleton ────────────────────────────────────────────────────
signal_memory = SignalMemory()


# ── Main public function ──────────────────────────────────────────────────────

def check_cross_tf_confirmation(
    symbol: str,
    timeframe: str,
    direction: str,
    timestamp: Optional[datetime] = None,
) -> str:
    """Record a signal and check for cross-TF confirmation.

    Parameters
    ----------
    symbol:    Asset symbol (e.g. "BTCUSD", "AAPL").
    timeframe: The signal's timeframe (e.g. "5m").
    direction: "buy" or "sell".
    timestamp: When the signal fired. Defaults to utcnow().

    Returns
    -------
    "CONFIRMED"  — an adjacent TF fired the same direction within 25 candles.
    "PENDING"    — stored; waiting for an adjacent TF to confirm.
    "EXPIRED"    — (reserved; cleanup handles expiry, not this call path).
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)

    # Normalise direction so "BUY"/"SELL" and "buy"/"sell" both work
    direction = direction.lower()

    confirmed = signal_memory.is_confirmed(symbol, timeframe, direction, timestamp)
    signal_memory.record(symbol, timeframe, direction, timestamp)

    if confirmed:
        return "CONFIRMED"
    return "PENDING"
