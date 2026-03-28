"""Database layer — Supabase (primary) with SQLite local fallback.

Tables managed here:
    buy_signals   — every BUY signal evaluated (valid or not)
    sell_signals  — every SELL signal evaluated (valid or not)
    trades        — all opened / closed trade records
    ml_features   — feature vectors derived from closed trades (for ML training)
    assets        — configurable asset universe override

On startup, call `init_db()` once.  All other calls are safe to make at any time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

log = logging.getLogger(__name__)

try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_LIB = True
except ImportError:
    _SUPABASE_LIB = False
    log.warning("supabase-py not installed — using SQLite only")

try:
    from src.config import SUPABASE_URL, SUPABASE_ANON_KEY, SQLITE_PATH, DATA_DIR
except ImportError:
    SUPABASE_URL      = ""
    SUPABASE_ANON_KEY = ""
    SQLITE_PATH       = "data/algobot.db"
    DATA_DIR          = Path("data")

# ── Supabase client ───────────────────────────────────────────────────────────

_sb_client: Optional[Any] = None

def _get_supabase() -> Optional[Any]:
    global _sb_client
    if _sb_client is not None:
        return _sb_client
    if _SUPABASE_LIB and SUPABASE_URL and SUPABASE_ANON_KEY:
        try:
            _sb_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
            log.info("Supabase connected: %s", SUPABASE_URL)
        except Exception as e:
            log.warning("Supabase connection failed (%s) — falling back to SQLite", e)
    return _sb_client


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _sqlite_path() -> Path:
    p = Path(SQLITE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _sqlite_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_sqlite_path(), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS buy_signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL,
        timeframe       TEXT    NOT NULL,
        timestamp       TEXT    NOT NULL,
        is_valid        INTEGER NOT NULL DEFAULT 0,
        points          INTEGER NOT NULL DEFAULT 0,
        alligator_pt    INTEGER NOT NULL DEFAULT 0,
        stochastic_pt   INTEGER NOT NULL DEFAULT 0,
        vortex_pt       INTEGER NOT NULL DEFAULT 0,
        staircase_ok    INTEGER NOT NULL DEFAULT 0,
        entry_price     REAL,
        stop_loss       REAL,
        ml_confidence   REAL,
        ai_confidence   REAL,
        rejection_reason TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sell_signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        asset           TEXT    NOT NULL,
        timeframe       TEXT    NOT NULL,
        timestamp       TEXT    NOT NULL,
        is_valid        INTEGER NOT NULL DEFAULT 0,
        points          INTEGER NOT NULL DEFAULT 0,
        alligator_pt    INTEGER NOT NULL DEFAULT 0,
        stochastic_pt   INTEGER NOT NULL DEFAULT 0,
        vortex_pt       INTEGER NOT NULL DEFAULT 0,
        staircase_ok    INTEGER NOT NULL DEFAULT 0,
        entry_price     REAL,
        stop_loss       REAL,
        ml_confidence   REAL,
        ai_confidence   REAL,
        rejection_reason TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id        TEXT    PRIMARY KEY,
        signal_type     TEXT    NOT NULL,
        asset           TEXT    NOT NULL,
        timeframe       TEXT    NOT NULL,
        entry_time      TEXT    NOT NULL,
        entry_price     REAL    NOT NULL,
        stop_loss_hard  REAL    NOT NULL,
        trailing_stop   REAL    NOT NULL,
        position_size   REAL    NOT NULL,
        account_risk_pct REAL   NOT NULL,
        jaw_at_entry    REAL,
        teeth_at_entry  REAL,
        lips_at_entry   REAL,
        ml_confidence   REAL,
        ai_confidence   REAL,
        exit_time       TEXT,
        exit_price      REAL,
        close_reason    TEXT,
        pnl             REAL    DEFAULT 0.0,
        pnl_pct         REAL    DEFAULT 0.0,
        max_trail_reached REAL  DEFAULT 0.0,
        status          TEXT    NOT NULL DEFAULT 'OPEN',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_features (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id        TEXT    NOT NULL REFERENCES trades(trade_id),
        features_json   TEXT    NOT NULL,
        outcome         REAL    NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        symbol          TEXT    PRIMARY KEY,
        asset_class     TEXT    NOT NULL,
        display_name    TEXT,
        is_active       INTEGER NOT NULL DEFAULT 1,
        source          TEXT,
        notes           TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
]


def init_db() -> None:
    """Create all tables if they do not exist. Safe to call multiple times."""
    with _sqlite_conn() as conn:
        for stmt in _CREATE_STATEMENTS:
            conn.execute(stmt)
    log.info("SQLite schema ready at %s", _sqlite_path())

    sb = _get_supabase()
    if sb:
        log.info("Supabase is available — writes will be mirrored")


# ── Signal persistence ────────────────────────────────────────────────────────

def _signal_row(sig: Any) -> dict:
    return {
        "asset":           sig.asset,
        "timeframe":       sig.timeframe,
        "timestamp":       sig.timestamp.isoformat(),
        "is_valid":        int(sig.is_valid),
        "points":          sig.points,
        "alligator_pt":    int(sig.alligator_point),
        "stochastic_pt":   int(sig.stochastic_point),
        "vortex_pt":       int(sig.vortex_point),
        # "staircase_ok" removed
        "entry_price":     sig.entry_price,
        "stop_loss":       sig.stop_loss,
        "ml_confidence":   sig.ml_confidence,
        "ai_confidence":   sig.ai_confidence,
        "rejection_reason":getattr(sig, "rejection_reason", ""),
    }


def save_signal(sig: Any) -> None:
    """Persist a BuySignalResult or SellSignalResult to DB."""
    table = "buy_signals" if sig.signal_type == "BUY" else "sell_signals"
    row   = _signal_row(sig)
    cols  = ", ".join(row.keys())
    placeholders = ", ".join("?" * len(row))

    with _sqlite_conn() as conn:
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))

    sb = _get_supabase()
    if sb:
        try:
            sb.table(table).insert(row).execute()
        except Exception as e:
            log.warning("Supabase signal insert failed: %s", e)


# ── Trade persistence ─────────────────────────────────────────────────────────

def save_trade_open(rec: Any) -> None:
    """Insert a newly opened trade (from TradeRecord)."""
    row = {
        "trade_id":        rec.trade_id,
        "signal_type":     rec.signal_type,
        "asset":           rec.asset,
        "timeframe":       rec.timeframe,
        "entry_time":      rec.entry_time.isoformat(),
        "entry_price":     rec.entry_price,
        "stop_loss_hard":  rec.stop_loss_hard,
        "trailing_stop":   rec.trailing_stop,
        "position_size":   rec.position_size,
        "account_risk_pct":rec.account_risk_pct,
        "jaw_at_entry":    rec.jaw_at_entry,
        "teeth_at_entry":  rec.teeth_at_entry,
        "lips_at_entry":   rec.lips_at_entry,
        "ml_confidence":   rec.ml_confidence,
        "ai_confidence":   rec.ai_confidence,
        "status":          "OPEN",
    }
    cols  = ", ".join(row.keys())
    ph    = ", ".join("?" * len(row))
    with _sqlite_conn() as conn:
        conn.execute(f"INSERT OR REPLACE INTO trades ({cols}) VALUES ({ph})", list(row.values()))
    sb = _get_supabase()
    if sb:
        try:
            sb.table("trades").upsert(row).execute()
        except Exception as e:
            log.warning("Supabase trade open failed: %s", e)


def save_trade_close(rec: Any) -> None:
    """Update an existing trade record as CLOSED."""
    with _sqlite_conn() as conn:
        conn.execute(
            """UPDATE trades SET exit_time=?, exit_price=?, close_reason=?,
               pnl=?, pnl_pct=?, max_trail_reached=?, trailing_stop=?, status='CLOSED'
               WHERE trade_id=?""",
            (
                rec.exit_time.isoformat() if rec.exit_time else None,
                rec.exit_price,
                rec.close_reason,
                rec.pnl,
                rec.pnl_pct,
                rec.max_trail_reached,
                rec.trailing_stop,
                rec.trade_id,
            ),
        )
    sb = _get_supabase()
    if sb:
        try:
            sb.table("trades").update({
                "exit_time":        rec.exit_time.isoformat() if rec.exit_time else None,
                "exit_price":       rec.exit_price,
                "close_reason":     rec.close_reason,
                "pnl":              rec.pnl,
                "pnl_pct":          rec.pnl_pct,
                "max_trail_reached":rec.max_trail_reached,
                "trailing_stop":    rec.trailing_stop,
                "status":           "CLOSED",
            }).eq("trade_id", rec.trade_id).execute()
        except Exception as e:
            log.warning("Supabase trade close failed: %s", e)


# ── Queries ───────────────────────────────────────────────────────────────────

def get_open_trades() -> list[dict]:
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_trades(limit: int = 1000) -> list[dict]:
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='CLOSED' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_signals(signal_type: str = "BUY", limit: int = 50) -> list[dict]:
    table = "buy_signals" if signal_type == "BUY" else "sell_signals"
    with _sqlite_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_ml_features(trade_id: str, features: dict, outcome: float) -> None:
    with _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO ml_features (trade_id, features_json, outcome) VALUES (?,?,?)",
            (trade_id, json.dumps(features), outcome),
        )


def get_ml_training_data() -> list[dict]:
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ml_features ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
