"""Database layer — Supabase (primary) with SQLite local fallback.

Tables managed here:
    buy_signals            — every BUY signal evaluated (valid or not)
    sell_signals           — every SELL signal evaluated (valid or not)
    trades                 — all opened / closed trade records (with lifecycle fields)
    trade_lifecycle_events — per-bar events: trail updates, MFE/MAE, break-even, lock stages
    ml_features            — feature vectors derived from closed trades (for ML training)
    assets                 — configurable asset universe override

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
        strategy_mode   TEXT    NOT NULL DEFAULT 'UNKNOWN',
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
        strategy_mode   TEXT    NOT NULL DEFAULT 'UNKNOWN',
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
        strategy_mode   TEXT    NOT NULL DEFAULT 'UNKNOWN',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_features (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id        TEXT    NOT NULL REFERENCES trades(trade_id),
        features_json   TEXT    NOT NULL,
        outcome         REAL    NOT NULL,
        feature_version INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ml_model_health (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp             TEXT    NOT NULL,
        model_type            TEXT    NOT NULL,  -- xgboost|lightgbm|ensemble
        is_loaded             INTEGER NOT NULL DEFAULT 0,
        avg_prediction_time_ms REAL   DEFAULT 0.0,
        predictions_count     INTEGER NOT NULL DEFAULT 0,
        errors_count          INTEGER NOT NULL DEFAULT 0,
        last_error_message    TEXT,
        created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_routing_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT    NOT NULL,
        trading_mode    TEXT    NOT NULL,
        asset           TEXT    NOT NULL,
        timeframe       TEXT,
        asset_class     TEXT,
        broker_name     TEXT    NOT NULL,
        reason          TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_executions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT    NOT NULL,
        broker_name     TEXT    NOT NULL,
        trade_id        TEXT,
        action          TEXT    NOT NULL,  -- place_order|close_order|modify_sltp
        asset           TEXT,
        timeframe       TEXT,
        request_json    TEXT,
        response_json   TEXT,
        ok              INTEGER NOT NULL DEFAULT 0,
        error_message   TEXT,
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
    """
    CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id            TEXT    NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
        event_time          TEXT    NOT NULL,
        event_type          TEXT    NOT NULL,
        trail_update_reason TEXT,
        old_value           REAL,
        new_value           REAL,
        current_price       REAL,
        profit_lock_stage   INTEGER DEFAULT 0,
        notes               TEXT,
        created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS optimization_proposals (
        proposal_id             TEXT    PRIMARY KEY,
        created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
        proposal_type           TEXT    NOT NULL,
        strategy_mode           TEXT,
        asset                   TEXT,
        asset_class             TEXT,
        current_value           TEXT,
        proposed_value          TEXT,
        reason_summary          TEXT    NOT NULL,
        evidence_summary        TEXT,
        evidence_metrics_json   TEXT,
        backtest_status         TEXT    NOT NULL DEFAULT 'pending',
        paper_validation_status TEXT    NOT NULL DEFAULT 'pending',
        approval_status         TEXT    NOT NULL DEFAULT 'draft',
        promoted_at             TEXT,
        superseded_by           TEXT    REFERENCES optimization_proposals(proposal_id)
    )
    """,
    # Phase 11: regime snapshots — one row per meaningful regime change per (asset, timeframe)
    """
    CREATE TABLE IF NOT EXISTS regime_snapshots (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        regime_id             TEXT    NOT NULL UNIQUE,
        created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
        asset                 TEXT    NOT NULL DEFAULT '',
        asset_class           TEXT    NOT NULL DEFAULT '',
        timeframe             TEXT    NOT NULL DEFAULT '',
        strategy_mode         TEXT    NOT NULL DEFAULT 'UNKNOWN',
        regime_label          TEXT    NOT NULL DEFAULT 'UNKNOWN',
        confidence_score      REAL    NOT NULL DEFAULT 0.0,
        evidence_summary      TEXT,
        volatility_json       TEXT,
        trend_json            TEXT,
        chop_json             TEXT,
        news_instability_flag INTEGER NOT NULL DEFAULT 0,
        news_source           TEXT,
        source_window         INTEGER NOT NULL DEFAULT 50
    )
    """,
    # Phase 14: active profile snapshot header
    """
    CREATE TABLE IF NOT EXISTS active_profile_snapshots (
        snapshot_id     TEXT    PRIMARY KEY,
        profile_name    TEXT    NOT NULL DEFAULT '',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        activated_at    TEXT,
        is_active       INTEGER NOT NULL DEFAULT 0,
        source_summary  TEXT,
        notes           TEXT
    )
    """,
    # Phase 14: active profile snapshot rules (detail rows)
    """
    CREATE TABLE IF NOT EXISTS active_profile_rules (
        rule_id             TEXT    PRIMARY KEY,
        snapshot_id         TEXT    NOT NULL REFERENCES active_profile_snapshots(snapshot_id),
        created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
        macro_regime        TEXT,
        regime_label        TEXT,
        strategy_mode       TEXT,
        asset               TEXT,
        asset_class         TEXT,
        suitability_rating  TEXT    NOT NULL DEFAULT 'UNKNOWN',
        suitability_score   REAL,
        mode_activation_state TEXT  NOT NULL DEFAULT 'ACTIVE',
        threshold_delta     REAL    NOT NULL DEFAULT 0.0,
        score_penalty       REAL    NOT NULL DEFAULT 0.0,
        block_entry         INTEGER NOT NULL DEFAULT 0,
        supporting_reason   TEXT,
        source_proposal_id  TEXT    REFERENCES optimization_proposals(proposal_id)
    )
    """,
]


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    """Add a column to an existing table only if it is not already present."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        log.info("Migration: added column %s.%s", table, column)


_TIMEFRAME_MODE_SQL = """
    CASE timeframe
        WHEN '3m'  THEN 'SCALP'
        WHEN '5m'  THEN 'SCALP'
        WHEN '15m' THEN 'INTERMEDIATE'
        WHEN '30m' THEN 'INTERMEDIATE'
        WHEN '1h'  THEN 'INTERMEDIATE'
        WHEN '2h'  THEN 'SWING'
        WHEN '3h'  THEN 'SWING'
        WHEN '4h'  THEN 'SWING'
        ELSE 'UNKNOWN'
    END
"""


def migrate_add_strategy_mode() -> None:
    """Add and backfill the strategy_mode column on all three signal/trade tables.

    Safe to call multiple times — skips columns that already exist and only
    updates rows where strategy_mode is still 'UNKNOWN'.
    """
    tables = [
        ("buy_signals",  "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("sell_signals", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("trades",       "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
    ]
    with _sqlite_conn() as conn:
        for table, col_type in tables:
            _add_column_if_missing(conn, table, "strategy_mode", col_type)
            conn.execute(
                f"UPDATE {table} SET strategy_mode = ({_TIMEFRAME_MODE_SQL})"
                " WHERE strategy_mode = 'UNKNOWN'"
            )
    log.info("Migration: strategy_mode backfill complete")


def migrate_add_lifecycle_fields() -> None:
    """Add Phase 3 lifecycle columns to the trades table.

    All columns default to NULL / 0 so existing rows keep their values.
    Safe to call multiple times — skips columns that already exist.
    """
    _LIFECYCLE_COLS: list[tuple[str, str]] = [
        # Phase 3
        ("entry_reason",                     "TEXT"),
        ("max_unrealized_profit",             "REAL    DEFAULT 0.0"),
        ("min_unrealized_profit",             "REAL    DEFAULT 0.0"),
        ("break_even_armed",                  "INTEGER DEFAULT 0"),
        ("profit_lock_stage",                 "INTEGER DEFAULT 0"),
        ("was_protected_profit",              "INTEGER DEFAULT 0"),
        ("timestamp_of_mfe",                  "TEXT"),
        ("timestamp_of_mae",                  "TEXT"),
        ("protected_profit_activation_time",  "TEXT"),
        ("initial_stop_value",                "REAL"),
        ("initial_exit_policy",               "TEXT"),
        ("exit_policy_name",                  "TEXT"),
        # Phase 4
        ("indicator_flags",                   "TEXT"),
        ("entry_reason_code",                 "TEXT"),
        ("trail_active_mode",                 "TEXT"),
        ("used_fallback_policy",              "INTEGER DEFAULT 0"),
    ]
    with _sqlite_conn() as conn:
        for col, col_type in _LIFECYCLE_COLS:
            _add_column_if_missing(conn, "trades", col, col_type)
    log.info("Migration: lifecycle fields ready on trades table")


def migrate_normalize_close_reasons() -> None:
    """Rename legacy close_reason values in the trades table.

    TRAILING_TP was the original label for PeakGiveback exits.  It was
    renamed to PEAK_GIVEBACK_EXIT in Phase 2 because the old label falsely
    implied a fixed take-profit and confused diagnostics.

    Safe to call multiple times — only updates rows that still carry the
    old label.
    """
    with _sqlite_conn() as conn:
        cur = conn.execute(
            "UPDATE trades SET close_reason = 'PEAK_GIVEBACK_EXIT'"
            " WHERE close_reason = 'TRAILING_TP'"
        )
        if cur.rowcount:
            log.info(
                "Migration: renamed %d legacy TRAILING_TP → PEAK_GIVEBACK_EXIT"
                " rows in trades",
                cur.rowcount,
            )


def init_db() -> None:
    """Create all tables if they do not exist. Safe to call multiple times."""
    with _sqlite_conn() as conn:
        for stmt in _CREATE_STATEMENTS:
            conn.execute(stmt)
    log.info("SQLite schema ready at %s", _sqlite_path())
    migrate_add_strategy_mode()
    migrate_normalize_close_reasons()
    migrate_add_lifecycle_fields()
    migrate_add_signal_intelligence_fields()
    migrate_add_fade_observability_fields()
    migrate_add_proposals_table()
    migrate_add_regime_snapshots_table()
    migrate_add_phase14_tables()
    migrate_add_prefilter_columns()

    sb = _get_supabase()
    if sb:
        log.info("Supabase is available — writes will be mirrored")


def migrate_add_signal_intelligence_fields() -> None:
    """Add Phase 5 score-breakdown and entry-intelligence columns to signal tables.

    Adds 13 columns to both ``buy_signals`` and ``sell_signals``.
    Safe to call multiple times — skips columns that already exist.
    Old rows receive default values (0 / NULL) automatically.
    """
    _COLS: list[tuple[str, str]] = [
        ("indicator_flags",              "TEXT"),
        ("entry_reason_code",            "TEXT"),
        ("accepted_signal",              "INTEGER DEFAULT 0"),
        ("score_total",                  "REAL    DEFAULT 0.0"),
        ("structure_points",             "REAL    DEFAULT 0.0"),
        ("indicator_points",             "REAL    DEFAULT 0.0"),
        ("timeframe_alignment_points",   "REAL    DEFAULT 0.0"),
        ("candle_quality_points",        "REAL    DEFAULT 0.0"),
        ("volatility_points",            "REAL    DEFAULT 0.0"),
        ("ml_adjustment_points",         "REAL    DEFAULT 0.0"),
        ("ml_effect",                    "TEXT"),
        ("ai_effect",                    "TEXT"),
    ]
    with _sqlite_conn() as conn:
        for table in ("buy_signals", "sell_signals"):
            for col, col_type in _COLS:
                _add_column_if_missing(conn, table, col, col_type)
    log.info("Migration: signal intelligence fields ready on buy/sell_signals tables")


def migrate_add_fade_observability_fields() -> None:
    """Add Phase 6 candle-fade observability columns to the trades table.

    Three new columns track how many times candle-trail tightening fired on
    each trade and the per-bar body/wick metrics captured at the last trigger.
    Safe to call multiple times — skips columns that already exist.
    """
    _COLS: list[tuple[str, str]] = [
        ("fade_tighten_count",   "INTEGER DEFAULT 0"),
        ("last_fade_body_ratio", "REAL"),
        ("last_fade_wick_ratio", "REAL"),
    ]
    with _sqlite_conn() as conn:
        for col, col_type in _COLS:
            _add_column_if_missing(conn, "trades", col, col_type)
    log.info("Migration: fade observability fields ready on trades table")


def migrate_add_proposals_table() -> None:
    """Ensure the optimization_proposals table exists (Phase 7).

    Uses ``CREATE TABLE IF NOT EXISTS`` so it is idempotent even for databases
    that were created before Phase 7.  The table is also included in
    ``_CREATE_STATEMENTS`` for fresh installations.
    """
    with _sqlite_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS optimization_proposals (
                proposal_id             TEXT    PRIMARY KEY,
                created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                proposal_type           TEXT    NOT NULL,
                strategy_mode           TEXT,
                asset                   TEXT,
                asset_class             TEXT,
                current_value           TEXT,
                proposed_value          TEXT,
                reason_summary          TEXT    NOT NULL,
                evidence_summary        TEXT,
                evidence_metrics_json   TEXT,
                backtest_status         TEXT    NOT NULL DEFAULT 'pending',
                paper_validation_status TEXT    NOT NULL DEFAULT 'pending',
                approval_status         TEXT    NOT NULL DEFAULT 'draft',
                promoted_at             TEXT,
                superseded_by           TEXT    REFERENCES optimization_proposals(proposal_id)
            )
            """
        )
    log.info("Migration: optimization_proposals table ready")


def migrate_add_regime_snapshots_table() -> None:
    """Ensure the regime_snapshots table exists (Phase 11).

    Uses ``CREATE TABLE IF NOT EXISTS`` so it is idempotent on databases
    created before Phase 11.  The table is also in ``_CREATE_STATEMENTS``
    for fresh installations.
    """
    with _sqlite_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_snapshots (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                regime_id             TEXT    NOT NULL UNIQUE,
                created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                asset                 TEXT    NOT NULL DEFAULT '',
                asset_class           TEXT    NOT NULL DEFAULT '',
                timeframe             TEXT    NOT NULL DEFAULT '',
                strategy_mode         TEXT    NOT NULL DEFAULT 'UNKNOWN',
                regime_label          TEXT    NOT NULL DEFAULT 'UNKNOWN',
                confidence_score      REAL    NOT NULL DEFAULT 0.0,
                evidence_summary      TEXT,
                volatility_json       TEXT,
                trend_json            TEXT,
                chop_json             TEXT,
                news_instability_flag INTEGER NOT NULL DEFAULT 0,
                news_source           TEXT,
                source_window         INTEGER NOT NULL DEFAULT 50
            )
            """
        )
    # Also add Phase 11 regime columns onto the trades table for outcome correlation
    _REGIME_TRADE_COLS: list[tuple[str, str]] = [
        ("regime_label_at_entry",      "TEXT"),
        ("regime_confidence_at_entry", "REAL DEFAULT 0.0"),
        ("regime_snapshot_id",         "TEXT"),
    ]
    with _sqlite_conn() as conn:
        for col, col_type in _REGIME_TRADE_COLS:
            _add_column_if_missing(conn, "trades", col, col_type)
    log.info("Migration: regime_snapshots table ready")

    # Phase 12: additional regime observability columns on trades
    _REGIME_P12_COLS: list[tuple[str, str]] = [
        ("regime_label_at_exit",         "TEXT"),
        ("regime_confidence_at_exit",    "REAL DEFAULT 0.0"),
        ("regime_changed_during_trade",  "INTEGER DEFAULT 0"),
        ("regime_transition_count",      "INTEGER DEFAULT 0"),
        ("regime_score_adjustment",      "REAL DEFAULT 0.0"),
    ]
    with _sqlite_conn() as conn:
        for col, col_type in _REGIME_P12_COLS:
            _add_column_if_missing(conn, "trades", col, col_type)
    log.info("Migration: Phase 12 regime trade columns ready")

    # Phase 13: macro_regime column on proposals for first-class regime queries
    with _sqlite_conn() as conn:
        _add_column_if_missing(conn, "optimization_proposals", "macro_regime", "TEXT")
    log.info("Migration: Phase 13 proposal regime column ready")


def migrate_add_phase14_tables() -> None:
    """Ensure Phase 14 live-suitability audit columns exist on signal tables.

    The ``active_profile_snapshots`` and ``active_profile_rules`` tables are
    created via ``_CREATE_STATEMENTS`` on fresh installs; this migration adds
    the nine signal-audit columns to pre-existing ``buy_signals`` /
    ``sell_signals`` tables.  Safe to call multiple times.
    """
    _SIGNAL_AUDIT_COLS: list[tuple[str, str]] = [
        ("macro_regime",               "TEXT"),
        ("regime_label",               "TEXT"),
        ("suitability_rating",         "TEXT"),
        ("suitability_score",          "REAL"),
        ("suitability_reason",         "TEXT"),
        ("suitability_source_summary", "TEXT"),
        ("skip_reason_code",           "TEXT"),
        ("decision_trace_json",        "TEXT"),
        ("active_profile_snapshot_id", "TEXT"),
    ]
    with _sqlite_conn() as conn:
        # Ensure the two new tables exist (idempotent on pre-Phase-14 DBs)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_profile_snapshots (
                snapshot_id     TEXT    PRIMARY KEY,
                profile_name    TEXT    NOT NULL DEFAULT '',
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                activated_at    TEXT,
                is_active       INTEGER NOT NULL DEFAULT 0,
                source_summary  TEXT,
                notes           TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_profile_rules (
                rule_id               TEXT    PRIMARY KEY,
                snapshot_id           TEXT    NOT NULL
                                          REFERENCES active_profile_snapshots(snapshot_id),
                created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
                macro_regime          TEXT,
                regime_label          TEXT,
                strategy_mode         TEXT,
                asset                 TEXT,
                asset_class           TEXT,
                suitability_rating    TEXT    NOT NULL DEFAULT 'UNKNOWN',
                suitability_score     REAL,
                mode_activation_state TEXT    NOT NULL DEFAULT 'ACTIVE',
                threshold_delta       REAL    NOT NULL DEFAULT 0.0,
                score_penalty         REAL    NOT NULL DEFAULT 0.0,
                block_entry           INTEGER NOT NULL DEFAULT 0,
                supporting_reason     TEXT,
                source_proposal_id    TEXT
                    REFERENCES optimization_proposals(proposal_id)
            )
            """
        )
        for table in ("buy_signals", "sell_signals"):
            for col, col_type in _SIGNAL_AUDIT_COLS:
                _add_column_if_missing(conn, table, col, col_type)
        # Phase 14: add suitability resolver columns to optimization_proposals
        _PROPOSAL_SUIT_COLS: list[tuple[str, str]] = [
            ("regime_label",    "TEXT"),
            ("suitability_score", "REAL"),
            ("threshold_delta", "REAL DEFAULT 0.0"),
            ("score_penalty",   "REAL DEFAULT 0.0"),
            ("block_entry",     "INTEGER DEFAULT 0"),
        ]
        for col, col_type in _PROPOSAL_SUIT_COLS:
            _add_column_if_missing(conn, "optimization_proposals", col, col_type)
    log.info("Migration: Phase 14 live-suitability columns ready")


def migrate_add_prefilter_columns() -> None:
    """Add Final Sprint prefilter audit columns to signal tables.

    Safe to call multiple times (idempotent).
    """
    _PREFILTER_COLS: list[tuple[str, str]] = [
        ("prefilter_universe_group", "TEXT"),
        ("prefilter_atr_pct",        "REAL"),
        ("prefilter_volume_ratio",   "REAL"),
        ("prefilter_rank_score",     "REAL"),
        ("prefilter_passed",         "INTEGER"),
        ("prefilter_skip_reason",    "TEXT"),
    ]
    with _sqlite_conn() as conn:
        for table in ("buy_signals", "sell_signals"):
            for col, col_type in _PREFILTER_COLS:
                _add_column_if_missing(conn, table, col, col_type)
    log.info("Migration: Final Sprint prefilter columns ready")


# ── Regime snapshot persistence (Phase 11) ───────────────────────────────────

def save_regime_snapshot(snapshot: Any) -> None:
    """Persist a RegimeSnapshot to regime_snapshots.

    Best-effort on Supabase; never raises.
    ``snapshot`` should be a ``RegimeSnapshot`` dataclass instance.
    """
    try:
        from dataclasses import asdict
        vm = snapshot.volatility_metrics
        tm = snapshot.trend_metrics
        cm = snapshot.chop_metrics
        row = {
            "regime_id":             snapshot.regime_id,
            "created_at":            snapshot.created_at.isoformat()
                                     if hasattr(snapshot.created_at, "isoformat")
                                     else str(snapshot.created_at),
            "asset":                 snapshot.asset,
            "asset_class":           snapshot.asset_class,
            "timeframe":             snapshot.timeframe,
            "strategy_mode":         snapshot.strategy_mode,
            "regime_label":          snapshot.regime_label.value
                                     if hasattr(snapshot.regime_label, "value")
                                     else str(snapshot.regime_label),
            "confidence_score":      float(snapshot.confidence_score),
            "evidence_summary":      snapshot.evidence_summary or "",
            "volatility_json":       json.dumps(asdict(vm)) if vm is not None else None,
            "trend_json":            json.dumps(asdict(tm)) if tm is not None else None,
            "chop_json":             json.dumps(asdict(cm)) if cm is not None else None,
            "news_instability_flag": int(bool(snapshot.news_instability_flag)),
            "news_source":           getattr(snapshot, "news_source", None),
            "source_window":         int(getattr(snapshot, "source_window", 50)),
        }
    except Exception as exc:
        log.debug("save_regime_snapshot: failed to build row: %s", exc)
        return

    try:
        with _sqlite_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO regime_snapshots
                   (regime_id, created_at, asset, asset_class, timeframe, strategy_mode,
                    regime_label, confidence_score, evidence_summary,
                    volatility_json, trend_json, chop_json,
                    news_instability_flag, news_source, source_window)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["regime_id"], row["created_at"], row["asset"], row["asset_class"],
                    row["timeframe"], row["strategy_mode"], row["regime_label"],
                    row["confidence_score"], row["evidence_summary"],
                    row["volatility_json"], row["trend_json"], row["chop_json"],
                    row["news_instability_flag"], row["news_source"], row["source_window"],
                ),
            )
    except Exception as exc:
        log.debug("save_regime_snapshot SQLite failed: %s", exc)
        return

    sb = _get_supabase()
    if sb:
        try:
            sb.table("regime_snapshots").upsert(row).execute()
        except Exception as exc:
            log.debug("Supabase regime_snapshots upsert failed: %s", exc)


def get_regime_snapshots(
    asset: str = "",
    timeframe: str = "",
    limit: int = 500,
) -> list[dict]:
    """Return regime snapshots, newest-first.

    If *asset* or *timeframe* are non-empty they are used as exact-match filters.
    """
    where: list[str] = []
    params: list[Any] = []
    if asset:
        where.append("asset = ?")
        params.append(asset)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    try:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM regime_snapshots {where_sql} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("get_regime_snapshots failed: %s", exc)
        return []


def get_latest_regime_snapshot(asset: str, timeframe: str) -> Optional[dict]:
    """Return the single most-recent regime snapshot for (asset, timeframe), or None."""
    try:
        with _sqlite_conn() as conn:
            row = conn.execute(
                "SELECT * FROM regime_snapshots WHERE asset=? AND timeframe=? "
                "ORDER BY created_at DESC LIMIT 1",
                (asset, timeframe),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        log.debug("get_latest_regime_snapshot failed: %s", exc)
        return None


# ── Phase 14: active profile snapshot persistence ─────────────────────────────

def save_profile_snapshot(header: dict) -> None:
    """Insert or replace an active_profile_snapshots row.  Best-effort; never raises."""
    try:
        cols = ", ".join(header.keys())
        ph   = ", ".join("?" * len(header))
        with _sqlite_conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO active_profile_snapshots ({cols}) VALUES ({ph})",
                list(header.values()),
            )
    except Exception as exc:
        log.warning("save_profile_snapshot failed: %s", exc)


def save_profile_rule(rule: dict) -> None:
    """Insert or replace an active_profile_rules row.  Best-effort; never raises."""
    try:
        cols = ", ".join(rule.keys())
        ph   = ", ".join("?" * len(rule))
        with _sqlite_conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO active_profile_rules ({cols}) VALUES ({ph})",
                list(rule.values()),
            )
    except Exception as exc:
        log.warning("save_profile_rule failed: %s", exc)


def get_active_profile_snapshot() -> Optional[dict]:
    """Return the single currently-active profile snapshot, or None."""
    try:
        with _sqlite_conn() as conn:
            row = conn.execute(
                "SELECT * FROM active_profile_snapshots WHERE is_active=1 "
                "ORDER BY activated_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        log.debug("get_active_profile_snapshot failed: %s", exc)
        return None


def get_active_profile_rules(snapshot_id: str) -> list[dict]:
    """Return all rules belonging to *snapshot_id*, unordered."""
    try:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM active_profile_rules WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("get_active_profile_rules failed: %s", exc)
        return []


def get_promoted_proposals_for_fallback() -> list[dict]:
    """Return promoted optimization proposals usable as suitability fallback rules.

    Only rows whose ``approval_status`` is ``'promoted'`` are returned;
    drafts, pending, and approved-but-not-promoted rows are intentionally
    excluded from live gating.
    """
    try:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM optimization_proposals "
                "WHERE approval_status='promoted' "
                "ORDER BY promoted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("get_promoted_proposals_for_fallback failed: %s", exc)
        return []


# ── Signal persistence ────────────────────────────────────────────────────────

def _float_or_none(v: Any) -> Any:
    """Convert to float if not None."""
    return float(v) if v is not None else None

def _int_or_none(v: Any) -> Any:
    """Convert to int if not None (for boolean → INTEGER mapping)."""
    return int(bool(v)) if v is not None else None

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
        "strategy_mode":   getattr(sig, "strategy_mode", "UNKNOWN"),
        # Phase 5: score breakdown + entry intelligence
        "indicator_flags":           getattr(sig, "indicator_flags",            None),
        "entry_reason_code":         getattr(sig, "entry_reason_code",          None),
        "accepted_signal":           int(bool(getattr(sig, "accepted_signal",   False))),
        "score_total":               float(getattr(sig, "score_total",          0.0)),
        "structure_points":          float(getattr(sig, "structure_points",     0.0)),
        "indicator_points":          float(getattr(sig, "indicator_points",     0.0)),
        "timeframe_alignment_points":float(getattr(sig, "timeframe_alignment_points", 0.0)),
        "candle_quality_points":     float(getattr(sig, "candle_quality_points", 0.0)),
        "volatility_points":         float(getattr(sig, "volatility_points",    0.0)),
        "ml_adjustment_points":      float(getattr(sig, "ml_adjustment_points", 0.0)),
        "ml_effect":                 getattr(sig, "ml_effect",                  None),
        "ai_effect":                 getattr(sig, "ai_effect",                  None),
        # Phase 14: live-suitability audit
        "macro_regime":               getattr(sig, "macro_regime",               None),
        "regime_label":               getattr(sig, "regime_label",               None),
        "suitability_rating":         getattr(sig, "suitability_rating",         None),
        "suitability_score":          getattr(sig, "suitability_score",          None),
        "suitability_reason":         getattr(sig, "suitability_reason",         None),
        "suitability_source_summary": getattr(sig, "suitability_source_summary", None),
        "skip_reason_code":           getattr(sig, "skip_reason_code",           None),
        "decision_trace_json":        getattr(sig, "decision_trace_json",        None),
        "active_profile_snapshot_id": getattr(sig, "active_profile_snapshot_id", None),
        # Final Sprint: prefilter audit
        "prefilter_universe_group":   getattr(sig, "prefilter_universe_group",   None),
        "prefilter_atr_pct":          _float_or_none(getattr(sig, "prefilter_atr_pct", None)),
        "prefilter_volume_ratio":     _float_or_none(getattr(sig, "prefilter_volume_ratio", None)),
        "prefilter_rank_score":       _float_or_none(getattr(sig, "prefilter_rank_score", None)),
        "prefilter_passed":           _int_or_none(getattr(sig, "prefilter_passed", None)),
        "prefilter_skip_reason":      getattr(sig, "prefilter_skip_reason",      None),
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
        "trade_id":           rec.trade_id,
        "signal_type":        rec.signal_type,
        "asset":              rec.asset,
        "timeframe":          rec.timeframe,
        "entry_time":         rec.entry_time.isoformat(),
        "entry_price":        rec.entry_price,
        "stop_loss_hard":     rec.stop_loss_hard,
        "trailing_stop":      rec.trailing_stop,
        "position_size":      rec.position_size,
        "account_risk_pct":   rec.account_risk_pct,
        "jaw_at_entry":       rec.jaw_at_entry,
        "teeth_at_entry":     rec.teeth_at_entry,
        "lips_at_entry":      rec.lips_at_entry,
        "ml_confidence":      rec.ml_confidence,
        "ai_confidence":      rec.ai_confidence,
        "status":             "OPEN",
        "strategy_mode":      getattr(rec, "strategy_mode", "UNKNOWN"),
        # Phase 3
        "entry_reason":       getattr(rec, "entry_reason", None),
        "initial_stop_value": getattr(rec, "initial_stop_value", None),
        "initial_exit_policy":getattr(rec, "initial_exit_policy", None),
        # Phase 4
        "indicator_flags":    getattr(rec, "indicator_flags", None),
        "entry_reason_code":  getattr(rec, "entry_reason_code", None),
        "trail_active_mode":  getattr(rec, "trail_active_mode", None),
        "used_fallback_policy": int(bool(getattr(rec, "used_fallback_policy", False))),
        # Phase 11
        "regime_label_at_entry":      getattr(rec, "regime_label_at_entry", None),
        "regime_confidence_at_entry": float(getattr(rec, "regime_confidence_at_entry", 0.0)),
        "regime_snapshot_id":         getattr(rec, "regime_snapshot_id", None),
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
    """Update an existing trade record as CLOSED (including all lifecycle fields)."""
    _dt = lambda v: v.isoformat() if v is not None else None
    with _sqlite_conn() as conn:
        conn.execute(
            """UPDATE trades
               SET exit_time=?, exit_price=?, close_reason=?,
                   pnl=?, pnl_pct=?, max_trail_reached=?, trailing_stop=?,
                   status='CLOSED',
                   max_unrealized_profit=?,
                   min_unrealized_profit=?,
                   break_even_armed=?,
                   profit_lock_stage=?,
                   was_protected_profit=?,
                   timestamp_of_mfe=?,
                   timestamp_of_mae=?,
                   protected_profit_activation_time=?,
                   exit_policy_name=?,
                   trail_active_mode=?,
                   used_fallback_policy=?,
                   fade_tighten_count=?,
                   last_fade_body_ratio=?,
                   last_fade_wick_ratio=?,
                   regime_label_at_exit=?,
                   regime_confidence_at_exit=?,
                   regime_changed_during_trade=?,
                   regime_transition_count=?,
                   regime_score_adjustment=?
               WHERE trade_id=?""",
            (
                _dt(rec.exit_time),
                rec.exit_price,
                rec.close_reason,
                rec.pnl,
                rec.pnl_pct,
                rec.max_trail_reached,
                rec.trailing_stop,
                getattr(rec, "max_unrealized_profit", 0.0),
                getattr(rec, "min_unrealized_profit", 0.0),
                int(bool(getattr(rec, "break_even_armed", False))),
                getattr(rec, "profit_lock_stage", 0),
                int(bool(getattr(rec, "was_protected_profit", False))),
                _dt(getattr(rec, "timestamp_of_mfe", None)),
                _dt(getattr(rec, "timestamp_of_mae", None)),
                _dt(getattr(rec, "protected_profit_activation_time", None)),
                getattr(rec, "exit_policy_name", None),
                getattr(rec, "trail_active_mode", None),
                int(bool(getattr(rec, "used_fallback_policy", False))),
                getattr(rec, "fade_tighten_count", 0),
                getattr(rec, "last_fade_body_ratio", None),
                getattr(rec, "last_fade_wick_ratio", None),
                getattr(rec, "regime_label_at_exit", None),
                getattr(rec, "regime_confidence_at_exit", 0.0),
                int(bool(getattr(rec, "regime_changed_during_trade", False))),
                getattr(rec, "regime_transition_count", 0),
                getattr(rec, "regime_score_adjustment", 0.0),
                rec.trade_id,
            ),
        )
    sb = _get_supabase()
    if sb:
        try:
            sb.table("trades").update({
                "exit_time":                      _dt(rec.exit_time),
                "exit_price":                     rec.exit_price,
                "close_reason":                   rec.close_reason,
                "pnl":                            rec.pnl,
                "pnl_pct":                        rec.pnl_pct,
                "max_trail_reached":               rec.max_trail_reached,
                "trailing_stop":                  rec.trailing_stop,
                "status":                         "CLOSED",
                "max_unrealized_profit":           getattr(rec, "max_unrealized_profit", 0.0),
                "min_unrealized_profit":           getattr(rec, "min_unrealized_profit", 0.0),
                "break_even_armed":                int(bool(getattr(rec, "break_even_armed", False))),
                "profit_lock_stage":               getattr(rec, "profit_lock_stage", 0),
                "was_protected_profit":            int(bool(getattr(rec, "was_protected_profit", False))),
                "timestamp_of_mfe":                _dt(getattr(rec, "timestamp_of_mfe", None)),
                "timestamp_of_mae":                _dt(getattr(rec, "timestamp_of_mae", None)),
                "protected_profit_activation_time":_dt(getattr(rec, "protected_profit_activation_time", None)),
                "exit_policy_name":               getattr(rec, "exit_policy_name", None),
                "trail_active_mode":              getattr(rec, "trail_active_mode", None),
                "used_fallback_policy":           int(bool(getattr(rec, "used_fallback_policy", False))),
                "fade_tighten_count":             getattr(rec, "fade_tighten_count", 0),
                "last_fade_body_ratio":           getattr(rec, "last_fade_body_ratio", None),
                "last_fade_wick_ratio":           getattr(rec, "last_fade_wick_ratio", None),
                "regime_label_at_exit":           getattr(rec, "regime_label_at_exit", None),
                "regime_confidence_at_exit":      getattr(rec, "regime_confidence_at_exit", 0.0),
                "regime_changed_during_trade":    int(bool(getattr(rec, "regime_changed_during_trade", False))),
                "regime_transition_count":        getattr(rec, "regime_transition_count", 0),
                "regime_score_adjustment":        getattr(rec, "regime_score_adjustment", 0.0),
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


# ── Lifecycle event persistence (Phase 3) ────────────────────────────────────

_VALID_TRAIL_REASONS = {
    "initial_stop", "break_even",
    "profit_lock_stage_1", "profit_lock_stage_2", "profit_lock_stage_3",
    "candle_trail", "manual_adjustment",
}

_VALID_EVENT_TYPES = {
    "trail_update", "break_even_armed", "profit_lock_stage",
    "mfe_update", "mae_update",
}


def save_lifecycle_event(
    trade_id:             str,
    event_type:           str,
    *,
    trail_update_reason:  Optional[str] = None,
    old_value:            Optional[float] = None,
    new_value:            Optional[float] = None,
    current_price:        Optional[float] = None,
    profit_lock_stage:    int = 0,
    notes:                str = "",
    event_time:           Optional[str] = None,
) -> None:
    """Insert one row into trade_lifecycle_events. Best-effort; never raises."""
    if event_type not in _VALID_EVENT_TYPES:
        log.debug("save_lifecycle_event: unknown event_type %r (allowed: %s)", event_type, _VALID_EVENT_TYPES)
    if trail_update_reason and trail_update_reason not in _VALID_TRAIL_REASONS:
        log.debug("save_lifecycle_event: unknown trail_update_reason %r", trail_update_reason)
    ts = event_time or datetime.utcnow().isoformat()
    row = {
        "trade_id":           trade_id,
        "event_time":         ts,
        "event_type":         event_type,
        "trail_update_reason":trail_update_reason,
        "old_value":          old_value,
        "new_value":          new_value,
        "current_price":      current_price,
        "profit_lock_stage":  profit_lock_stage,
        "notes":              notes or None,
    }
    try:
        with _sqlite_conn() as conn:
            conn.execute(
                """INSERT INTO trade_lifecycle_events
                   (trade_id, event_time, event_type, trail_update_reason,
                    old_value, new_value, current_price, profit_lock_stage, notes)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (row["trade_id"], row["event_time"], row["event_type"],
                 row["trail_update_reason"], row["old_value"], row["new_value"],
                 row["current_price"], row["profit_lock_stage"], row["notes"]),
            )
    except Exception as e:
        log.debug("save_lifecycle_event failed: %s", e)
        return
    sb = _get_supabase()
    if sb:
        try:
            sb.table("trade_lifecycle_events").insert(row).execute()
        except Exception as e:
            log.debug("Supabase lifecycle event insert failed: %s", e)


def update_trade_lifecycle(trade_id: str, **fields: Any) -> None:
    """UPDATE a subset of lifecycle columns on an open trade mid-lifecycle.

    Only whitelisted column names are written to prevent SQL injection.
    Call this to push MFE/MAE/break-even/lock-stage changes to DB between
    the open and close writes.
    """
    _ALLOWED = {
        # Phase 3
        "max_unrealized_profit", "min_unrealized_profit",
        "break_even_armed", "profit_lock_stage", "was_protected_profit",
        "timestamp_of_mfe", "timestamp_of_mae",
        "protected_profit_activation_time", "trailing_stop",
        # Phase 4
        "trail_active_mode", "used_fallback_policy", "exit_policy_name",
        # Phase 6
        "fade_tighten_count", "last_fade_body_ratio", "last_fade_wick_ratio",
        # Phase 11
        "regime_label_at_entry", "regime_confidence_at_entry", "regime_snapshot_id",
    }
    safe = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not safe:
        return
    set_clause = ", ".join(f"{k}=?" for k in safe)
    values = list(safe.values()) + [trade_id]
    try:
        with _sqlite_conn() as conn:
            conn.execute(
                f"UPDATE trades SET {set_clause} WHERE trade_id=?", values
            )
    except Exception as e:
        log.debug("update_trade_lifecycle failed: %s", e)
        return
    sb = _get_supabase()
    if sb:
        try:
            sb.table("trades").update(safe).eq("trade_id", trade_id).execute()
        except Exception as e:
            log.debug("Supabase update_trade_lifecycle failed: %s", e)


def get_trade_forensic(trade_id: str) -> dict:
    """Return the full trade row + all lifecycle events for a single trade.

    Returns:
        {
            "trade": dict | None,      # None when trade_id not found
            "events": list[dict],      # ordered by event_time ASC
        }
    """
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT * FROM trade_lifecycle_events WHERE trade_id=? ORDER BY event_time ASC",
            (trade_id,),
        ).fetchall()
    return {
        "trade":  dict(row) if row else None,
        "events": [dict(e) for e in events],
    }


def get_recent_signals(signal_type: str = "BUY", limit: int = 50) -> list[dict]:
    table = "buy_signals" if signal_type == "BUY" else "sell_signals"
    with _sqlite_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_ml_features(trade_id: str, features: dict, outcome: float) -> None:
    """Persist an ML training row.

    ``features`` may be either:
      - a list-like vector in canonical feature order, OR
      - a dict with numeric string keys ("0","1",...) mapping to floats (legacy).
    """
    row = {
        "trade_id": trade_id,
        "features_json": json.dumps(features),
        "outcome": float(outcome),
        "feature_version": 1,
        "created_at": datetime.utcnow().isoformat(),
    }

    with _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO ml_features (trade_id, features_json, outcome, feature_version, created_at) VALUES (?,?,?,?,?)",
            (row["trade_id"], row["features_json"], row["outcome"], row["feature_version"], row["created_at"]),
        )

    sb = _get_supabase()
    if sb:
        try:
            sb.table("ml_features").insert(row).execute()
        except Exception as e:
            log.warning("Supabase ml_features insert failed: %s", e)


def save_ml_model_health(
    *,
    model_type: str,
    is_loaded: bool,
    avg_prediction_time_ms: float,
    predictions_count: int,
    errors_count: int,
    last_error_message: str = "",
    timestamp: Optional[str] = None,
) -> None:
    """Persist ML model health metrics (Supabase + SQLite)."""
    ts = timestamp or datetime.utcnow().isoformat()
    row = {
        "timestamp": ts,
        "model_type": model_type,
        "is_loaded": int(bool(is_loaded)),
        "avg_prediction_time_ms": float(avg_prediction_time_ms),
        "predictions_count": int(predictions_count),
        "errors_count": int(errors_count),
        "last_error_message": last_error_message or "",
        "created_at": datetime.utcnow().isoformat(),
    }

    with _sqlite_conn() as conn:
        conn.execute(
            """INSERT INTO ml_model_health
               (timestamp, model_type, is_loaded, avg_prediction_time_ms, predictions_count, errors_count, last_error_message, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                row["timestamp"], row["model_type"], row["is_loaded"],
                row["avg_prediction_time_ms"], row["predictions_count"], row["errors_count"],
                row["last_error_message"], row["created_at"],
            ),
        )

    sb = _get_supabase()
    if sb:
        try:
            sb.table("ml_model_health").insert(row).execute()
        except Exception as e:
            log.warning("Supabase ml_model_health insert failed: %s", e)


def save_broker_routing_decision(
    *,
    trading_mode: str,
    asset: str,
    timeframe: str = "",
    asset_class: str = "",
    broker_name: str,
    reason: str = "",
    timestamp: Optional[str] = None,
) -> None:
    ts = timestamp or datetime.utcnow().isoformat()
    row = {
        "timestamp": ts,
        "trading_mode": trading_mode,
        "asset": asset,
        "timeframe": timeframe,
        "asset_class": asset_class,
        "broker_name": broker_name,
        "reason": reason,
        "created_at": datetime.utcnow().isoformat(),
    }
    with _sqlite_conn() as conn:
        conn.execute(
            """INSERT INTO broker_routing_decisions
               (timestamp, trading_mode, asset, timeframe, asset_class, broker_name, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                row["timestamp"], row["trading_mode"], row["asset"], row["timeframe"],
                row["asset_class"], row["broker_name"], row["reason"], row["created_at"],
            ),
        )
    sb = _get_supabase()
    if sb:
        try:
            sb.table("broker_routing_decisions").insert(row).execute()
        except Exception as e:
            log.warning("Supabase broker_routing_decisions insert failed: %s", e)


def save_broker_execution(
    *,
    broker_name: str,
    action: str,
    ok: bool,
    trade_id: str = "",
    asset: str = "",
    timeframe: str = "",
    request: Optional[dict] = None,
    response: Optional[dict] = None,
    error_message: str = "",
    timestamp: Optional[str] = None,
) -> None:
    ts = timestamp or datetime.utcnow().isoformat()
    row = {
        "timestamp": ts,
        "broker_name": broker_name,
        "trade_id": trade_id or None,
        "action": action,
        "asset": asset,
        "timeframe": timeframe,
        "request_json": json.dumps(request or {}),
        "response_json": json.dumps(response or {}),
        "ok": int(bool(ok)),
        "error_message": error_message or "",
        "created_at": datetime.utcnow().isoformat(),
    }
    with _sqlite_conn() as conn:
        conn.execute(
            """INSERT INTO broker_executions
               (timestamp, broker_name, trade_id, action, asset, timeframe, request_json, response_json, ok, error_message, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["timestamp"], row["broker_name"], row["trade_id"], row["action"],
                row["asset"], row["timeframe"], row["request_json"], row["response_json"],
                row["ok"], row["error_message"], row["created_at"],
            ),
        )
    sb = _get_supabase()
    if sb:
        try:
            sb.table("broker_executions").insert(row).execute()
        except Exception as e:
            log.warning("Supabase broker_executions insert failed: %s", e)


def get_ml_training_data() -> list[dict]:
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ml_features ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Proposal persistence (Phase 7) ───────────────────────────────────────────

_VALID_PROPOSAL_TYPES = frozenset({
    "threshold_change",
    "ml_veto_change",
    "ai_veto_change",
    "indicator_combo_penalty",
    "indicator_combo_bonus",
    "candle_fade_requirement_change",
    "asset_specific_threshold",
    "mode_specific_threshold",
    "exit_policy_tightening",
    "exit_policy_relaxation",
    # Phase 13: regime-aware proposal types
    "regime_threshold_change",
    "regime_exit_policy_change",
    "regime_fade_requirement_change",
    "regime_ml_veto_change",
    "regime_ai_veto_change",
})

_VALID_PROPOSAL_STATUSES = frozenset({
    "draft",
    "backtest_pending",
    "backtest_complete",
    "paper_validation_pending",
    "paper_validation_complete",
    "approved",
    "rejected",
    "promoted",
    "superseded",
})

# Enforces safe manual-approval gate: "promoted" is only reachable from "approved"
_PROPOSAL_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft":                     frozenset({"backtest_pending",         "rejected"}),
    "backtest_pending":          frozenset({"backtest_complete",        "rejected"}),
    "backtest_complete":         frozenset({"paper_validation_pending", "rejected"}),
    "paper_validation_pending":  frozenset({"paper_validation_complete","rejected"}),
    "paper_validation_complete": frozenset({"approved",                 "rejected"}),
    "approved":                  frozenset({"promoted", "superseded",   "rejected"}),
    "promoted":                  frozenset({"superseded"}),
    "rejected":                  frozenset(),   # terminal
    "superseded":                frozenset(),   # terminal
}


def save_proposal(proposal: dict) -> None:
    """Persist one proposal row to ``optimization_proposals``.

    *proposal* must contain at minimum ``proposal_id``, ``proposal_type``, and
    ``reason_summary``.  All other columns receive their DEFAULT values when
    absent.

    Raises ``ValueError`` for unknown ``proposal_type`` or ``approval_status``
    values so bad data is caught at write time.
    """
    ptype  = proposal.get("proposal_type", "")
    status = proposal.get("approval_status", "draft")
    if ptype not in _VALID_PROPOSAL_TYPES:
        raise ValueError(f"Unknown proposal_type: {ptype!r}")
    if status not in _VALID_PROPOSAL_STATUSES:
        raise ValueError(f"Unknown approval_status: {status!r}")

    cols = [
        "proposal_id", "created_at", "proposal_type", "strategy_mode",
        "asset", "asset_class", "macro_regime", "current_value", "proposed_value",
        "reason_summary", "evidence_summary", "evidence_metrics_json",
        "backtest_status", "paper_validation_status", "approval_status",
        "promoted_at", "superseded_by",
    ]
    defaults: dict[str, Any] = {
        "created_at":              datetime.utcnow().isoformat(),
        "backtest_status":         "pending",
        "paper_validation_status": "pending",
        "approval_status":         "draft",
    }
    row = {c: proposal.get(c, defaults.get(c)) for c in cols}

    ph = ", ".join("?" * len(cols))
    col_str = ", ".join(cols)
    with _sqlite_conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO optimization_proposals ({col_str}) VALUES ({ph})",
            [row[c] for c in cols],
        )
    log.debug("Saved proposal %s (%s)", row["proposal_id"], row["proposal_type"])


def get_proposals(
    *,
    status: Optional[str] = None,
    strategy_mode: Optional[str] = None,
    proposal_type: Optional[str] = None,
    macro_regime: Optional[str] = None,
) -> list[dict]:
    """Return proposals from ``optimization_proposals`` with optional filters.

    All filters are ANDed together.  Omit a filter to return all values for
    that dimension.

    Returns
    -------
    list of dicts ordered by ``created_at`` DESC.
    """
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("approval_status = ?")
        params.append(status)
    if strategy_mode is not None:
        where.append("strategy_mode = ?")
        params.append(strategy_mode)
    if proposal_type is not None:
        where.append("proposal_type = ?")
        params.append(proposal_type)
    if macro_regime is not None:
        where.append("macro_regime = ?")
        params.append(macro_regime)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM optimization_proposals {where_sql} ORDER BY created_at DESC"

    try:
        with _sqlite_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("get_proposals failed: %s", exc)
        return []


def transition_proposal_status(proposal_id: str, new_status: str) -> None:
    """Move *proposal_id* to *new_status* if the transition is valid.

    Raises ``ValueError`` for invalid transitions so callers cannot bypass the
    approval gate.  The ``"promoted"`` status is only reachable from
    ``"approved"``; no shortcut exists.
    """
    if new_status not in _VALID_PROPOSAL_STATUSES:
        raise ValueError(f"Unknown proposal status: {new_status!r}")

    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT approval_status FROM optimization_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Proposal not found: {proposal_id!r}")

    current = row["approval_status"]
    allowed = _PROPOSAL_VALID_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition proposal {proposal_id!r} from {current!r} to {new_status!r}. "
            f"Allowed next states: {sorted(allowed) or ['(none — terminal)']}"
        )

    extra: dict[str, Any] = {}
    if new_status == "promoted":
        extra["promoted_at"] = datetime.utcnow().isoformat()

    set_parts = ["approval_status = ?"]
    set_vals: list[Any] = [new_status]
    for col, val in extra.items():
        set_parts.append(f"{col} = ?")
        set_vals.append(val)
    set_vals.append(proposal_id)

    with _sqlite_conn() as conn:
        conn.execute(
            f"UPDATE optimization_proposals SET {', '.join(set_parts)} WHERE proposal_id = ?",
            set_vals,
        )
    log.info("Proposal %s transitioned %s → %s", proposal_id, current, new_status)


def get_proposals_summary() -> dict:
    """Return counts of proposals grouped by ``approval_status``.

    Returns
    -------
    dict mapping each known status string to its count.  Status values with
    zero proposals are omitted.
    """
    try:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                "SELECT approval_status, COUNT(*) AS cnt"
                " FROM optimization_proposals GROUP BY approval_status"
            ).fetchall()
        return {r["approval_status"]: r["cnt"] for r in rows}
    except Exception as exc:
        log.warning("get_proposals_summary failed: %s", exc)
        return {}
