"""Cross-Timeframe Signal Confirmation — SQLite-backed shared memory.

Each timeframe bot instance (1m, 2m, 3m …) is a separate process.
In-memory stores don't survive across processes, so signal state is
persisted in the shared SQLite database (data/algobot.db).

A signal is CONFIRMED when two adjacent timeframes both show the same
direction within a 50-candle window of each other.

Adjacent pairs (either order confirms):
  1m  ↔ 2m, 3m
  2m  ↔ 1m, 3m
  3m  ↔ 2m, 5m
  5m  ↔ 3m, 15m
  15m ↔ 5m, 30m
  30m ↔ 15m, 1h
  1h  ↔ 30m, 2h
  2h  ↔ 1h, 4h
  4h  ↔ 2h, 1d

If an adjacent TF has no data, the next available one is tried automatically.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Adjacent timeframe map ────────────────────────────────────────────────────
_ADJACENT_TFS: dict[str, list[str]] = {
    "1m":  ["2m", "3m"],
    "2m":  ["1m", "3m"],
    "3m":  ["2m", "5m"],
    "5m":  ["3m", "15m"],
    "15m": ["5m", "30m"],
    "30m": ["15m", "1h"],
    "1h":  ["30m", "2h"],
    "2h":  ["1h", "4h"],
    "4h":  ["2h", "1d"],
}

# ── Minutes per candle ────────────────────────────────────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "2m": 2, "3m": 3, "5m": 5,
    "15m": 15, "30m": 30, "1h": 60,
    "2h": 120, "4h": 240, "1d": 1440,
}

_WINDOW_CANDLES = 50   # each TF confirmation window in its own candle units


# ── SQLite path (mirrors db.py logic) ─────────────────────────────────────────

def _db_path() -> Path:
    try:
        from src.config import SQLITE_PATH
        return Path(SQLITE_PATH)
    except Exception:
        return Path("data/algobot.db")


# ── Shared signal memory (SQLite) ─────────────────────────────────────────────

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mtf_signal_memory (
            symbol    TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT NOT NULL,
            fired_at  REAL NOT NULL,          -- Unix timestamp (UTC)
            PRIMARY KEY (symbol, timeframe, direction)
        )
    """)
    conn.commit()


def _record(symbol: str, timeframe: str, direction: str, ts: datetime) -> None:
    """Upsert a signal into the shared SQLite table."""
    fired_at = ts.timestamp()
    try:
        with sqlite3.connect(_db_path()) as conn:
            _ensure_table(conn)
            conn.execute("""
                INSERT INTO mtf_signal_memory (symbol, timeframe, direction, fired_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, direction)
                DO UPDATE SET fired_at = excluded.fired_at
            """, (symbol, timeframe, direction.lower(), fired_at))
            conn.commit()
    except Exception as exc:
        log.debug("MTF record failed: %s", exc)


def _is_confirmed(
    symbol: str,
    timeframe: str,
    direction: str,
    ts: datetime,
) -> Optional[str]:
    """Return the confirming adjacent TF name, or None if not confirmed."""
    direction = direction.lower()
    tf_mins = _TF_MINUTES.get(timeframe, 1)
    window_seconds = _WINDOW_CANDLES * tf_mins * 60
    now_ts = ts.timestamp()
    cutoff = now_ts - window_seconds

    adjacent = _ADJACENT_TFS.get(timeframe, [])
    try:
        with sqlite3.connect(_db_path()) as conn:
            _ensure_table(conn)
            for adj_tf in adjacent:
                row = conn.execute("""
                    SELECT fired_at FROM mtf_signal_memory
                    WHERE symbol=? AND timeframe=? AND direction=? AND fired_at >= ?
                """, (symbol, adj_tf, direction, cutoff)).fetchone()
                if row:
                    age = now_ts - row[0]
                    log.debug(
                        "MTF CONFIRMED: %s %s %s — adjacent %s fired %.0fs ago (window %ds)",
                        symbol, timeframe, direction, adj_tf, age, window_seconds,
                    )
                    return adj_tf
    except Exception as exc:
        log.debug("MTF confirm check failed: %s", exc)
    return None


def cleanup_expired() -> None:
    """Remove entries older than 50 candles on their own timeframe."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            _ensure_table(conn)
            now = datetime.now(tz=timezone.utc).timestamp()
            # Use the largest window (1d = 1440 * 50 = 72000s) as safe max
            # Each TF cleans its own expired entries
            for tf, mins in _TF_MINUTES.items():
                cutoff = now - (_WINDOW_CANDLES * mins * 60)
                conn.execute("""
                    DELETE FROM mtf_signal_memory
                    WHERE timeframe=? AND fired_at < ?
                """, (tf, cutoff))
            conn.commit()
    except Exception as exc:
        log.debug("MTF cleanup failed: %s", exc)


# ── Main public function ──────────────────────────────────────────────────────

def check_cross_tf_confirmation(
    symbol: str,
    timeframe: str,
    direction: str,
    timestamp: Optional[datetime] = None,
) -> str:
    """Record a signal and check for cross-TF confirmation via shared SQLite.

    Returns
    -------
    "CONFIRMED"  — an adjacent TF fired the same direction within the window.
    "PENDING"    — stored; waiting for an adjacent TF to confirm.
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)

    direction = direction.lower()

    # Check BEFORE recording so we don't self-confirm
    confirmed_tf = _is_confirmed(symbol, timeframe, direction, timestamp)
    _record(symbol, timeframe, direction, timestamp)

    if confirmed_tf:
        # Return "CONFIRMED:adj_tf" so callers can display which pair aligned
        return f"CONFIRMED:{confirmed_tf}"
    return "PENDING"


# ── Backwards-compat shim (scanner imports signal_memory.cleanup) ─────────────
class _MemoryShim:
    def cleanup(self) -> None:
        cleanup_expired()

signal_memory = _MemoryShim()
