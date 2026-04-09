"""Tests for Phase 3 lifecycle DB functions and TradeRecord fields.

Exercises:
  - TradeRecord has all 14 new fields with correct defaults
  - migrate_add_lifecycle_fields() is idempotent on a fresh in-memory DB
  - save_trade_open writes entry_reason / initial_stop_value / initial_exit_policy
  - save_lifecycle_event inserts and is retrievable via get_trade_forensic
  - update_trade_lifecycle updates only allowed columns
  - MFE/MAE logic: correct max/min tracking over sequential bar updates
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.signals.types import TradeRecord


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_rec(**kwargs) -> TradeRecord:
    defaults = dict(
        trade_id         = str(uuid.uuid4()),
        signal_type      = "BUY",
        asset            = "BTCUSDT",
        timeframe        = "15m",
        entry_time       = datetime.now(),
        entry_price      = 100.0,
        stop_loss_hard   = 98.0,
        trailing_stop    = 98.0,
        position_size    = 1.0,
        account_risk_pct = 1.0,
        alligator_point  = True,
        stochastic_point = True,
        vortex_point     = True,
        jaw_at_entry     = 97.0,
        teeth_at_entry   = 98.5,
        lips_at_entry    = 100.5,
    )
    defaults.update(kwargs)
    return TradeRecord(**defaults)


# ── TradeRecord field tests ────────────────────────────────────────────────────

class TestTradeRecordLifecycleFields:
    def test_all_new_fields_exist(self):
        rec = _make_rec()
        assert hasattr(rec, "entry_reason")
        assert hasattr(rec, "max_unrealized_profit")
        assert hasattr(rec, "min_unrealized_profit")
        assert hasattr(rec, "break_even_armed")
        assert hasattr(rec, "profit_lock_stage")
        assert hasattr(rec, "was_protected_profit")
        assert hasattr(rec, "trail_update_reason")
        assert hasattr(rec, "timestamp_of_mfe")
        assert hasattr(rec, "timestamp_of_mae")
        assert hasattr(rec, "protected_profit_activation_time")
        assert hasattr(rec, "initial_stop_value")
        assert hasattr(rec, "initial_exit_policy")
        assert hasattr(rec, "exit_policy_name")

    def test_new_fields_default_to_none_or_zero(self):
        rec = _make_rec()
        assert rec.entry_reason is None
        assert rec.max_unrealized_profit == 0.0
        assert rec.min_unrealized_profit == 0.0
        assert rec.break_even_armed is False
        assert rec.profit_lock_stage == 0
        assert rec.was_protected_profit is False
        assert rec.trail_update_reason is None
        assert rec.timestamp_of_mfe is None
        assert rec.timestamp_of_mae is None
        assert rec.protected_profit_activation_time is None
        assert rec.initial_stop_value is None
        assert rec.initial_exit_policy is None
        assert rec.exit_policy_name is None

    def test_existing_fields_unaffected(self):
        """Phase 1/2 fields must still construct without supplying new ones."""
        rec = _make_rec()
        assert rec.status == "OPEN"
        assert rec.strategy_mode == "UNKNOWN"
        assert rec.pnl == 0.0
        assert rec.close_reason is None


# ── DB / migration tests (using in-memory SQLite via monkeypatching) ──────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """Patch SQLITE_PATH to an isolated temp file; re-import db module each test."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("SQLITE_PATH", db_file)

    import importlib
    import src.data.db as db_mod
    # Patch the path resolver
    monkeypatch.setattr(db_mod, "SQLITE_PATH", db_file)

    # Ensure fresh module state for each test
    db_mod._sb_client = None
    db_mod.init_db()
    return db_mod


class TestMigration:
    def test_migrate_lifecycle_fields_idempotent(self, _db):
        """Running the migration twice must not raise or duplicate columns."""
        _db.migrate_add_lifecycle_fields()
        _db.migrate_add_lifecycle_fields()  # second call — must be silent

    def test_lifecycle_columns_present_after_migration(self, _db):
        with _db._sqlite_conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        for expected in (
            "entry_reason", "max_unrealized_profit", "min_unrealized_profit",
            "break_even_armed", "profit_lock_stage", "was_protected_profit",
            "timestamp_of_mfe", "timestamp_of_mae", "protected_profit_activation_time",
            "initial_stop_value", "initial_exit_policy", "exit_policy_name",
        ):
            assert expected in cols, f"Missing column: {expected}"

    def test_trade_lifecycle_events_table_created(self, _db):
        with _db._sqlite_conn() as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "trade_lifecycle_events" in tables


class TestSaveTradeOpenLifecycle:
    def test_entry_reason_persisted(self, _db):
        rec = _make_rec(entry_reason="alligator+stochastic | ml=85% ai=70%",
                        initial_stop_value=98.0,
                        initial_exit_policy="SCALP")
        _db.save_trade_open(rec)
        with _db._sqlite_conn() as conn:
            row = dict(conn.execute(
                "SELECT entry_reason, initial_stop_value, initial_exit_policy FROM trades WHERE trade_id=?",
                (rec.trade_id,)
            ).fetchone())
        assert row["entry_reason"] == "alligator+stochastic | ml=85% ai=70%"
        assert row["initial_stop_value"] == pytest.approx(98.0)
        assert row["initial_exit_policy"] == "SCALP"

    def test_save_without_new_fields_does_not_crash(self, _db):
        """Old-style TradeRecord with no lifecycle fields must still persist."""
        rec = _make_rec()
        _db.save_trade_open(rec)


class TestSaveLifecycleEvent:
    def test_event_inserted_and_retrievable(self, _db):
        rec = _make_rec()
        _db.save_trade_open(rec)
        _db.save_lifecycle_event(
            rec.trade_id, "trail_update",
            trail_update_reason="initial_stop",
            old_value=None,
            new_value=98.0,
            current_price=100.0,
        )
        forensic = _db.get_trade_forensic(rec.trade_id)
        events = forensic["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "trail_update"
        assert events[0]["trail_update_reason"] == "initial_stop"
        assert events[0]["new_value"] == pytest.approx(98.0)

    def test_multiple_events_ordered_by_time(self, _db):
        rec = _make_rec()
        _db.save_trade_open(rec)
        for i, event_type in enumerate(["trail_update", "mfe_update", "mae_update"]):
            _db.save_lifecycle_event(rec.trade_id, event_type, new_value=float(i))
        forensic = _db.get_trade_forensic(rec.trade_id)
        types = [e["event_type"] for e in forensic["events"]]
        assert types == ["trail_update", "mfe_update", "mae_update"]

    def test_unknown_event_type_does_not_raise(self, _db):
        """save_lifecycle_event is best-effort; unknown types should not crash."""
        rec = _make_rec()
        _db.save_trade_open(rec)
        _db.save_lifecycle_event(rec.trade_id, "unknown_future_type", new_value=1.0)


class TestUpdateTradeLifecycle:
    def test_whitelisted_fields_updated(self, _db):
        rec = _make_rec()
        _db.save_trade_open(rec)
        _db.update_trade_lifecycle(
            rec.trade_id,
            max_unrealized_profit=2.5,
            break_even_armed=1,
        )
        with _db._sqlite_conn() as conn:
            row = dict(conn.execute(
                "SELECT max_unrealized_profit, break_even_armed FROM trades WHERE trade_id=?",
                (rec.trade_id,)
            ).fetchone())
        assert row["max_unrealized_profit"] == pytest.approx(2.5)
        assert row["break_even_armed"] == 1

    def test_non_whitelisted_field_ignored(self, _db):
        """Passing a non-whitelisted field must not cause an SQL injection / error."""
        rec = _make_rec()
        _db.save_trade_open(rec)
        # This should be silently ignored, not raise
        _db.update_trade_lifecycle(rec.trade_id, malicious_col="DROP TABLE trades;--")


class TestGetTradeForesic:
    def test_returns_none_trade_for_missing_id(self, _db):
        result = _db.get_trade_forensic("nonexistent-id")
        assert result["trade"] is None
        assert result["events"] == []

    def test_closed_trade_fields_persisted(self, _db):
        rec = _make_rec(
            max_unrealized_profit=1.8,
            min_unrealized_profit=-0.5,
            break_even_armed=True,
            profit_lock_stage=2,
            was_protected_profit=True,
            exit_policy_name="SCALP | stage_2_locked",
        )
        rec.exit_time    = datetime.now()
        rec.exit_price   = 101.5
        rec.close_reason = "PEAK_GIVEBACK_EXIT"
        rec.pnl          = 1.5
        rec.pnl_pct      = 1.5
        rec.status       = "CLOSED"

        _db.save_trade_open(rec)
        _db.save_trade_close(rec)

        forensic = _db.get_trade_forensic(rec.trade_id)
        trade = forensic["trade"]
        assert trade["max_unrealized_profit"] == pytest.approx(1.8)
        assert trade["break_even_armed"] == 1
        assert trade["profit_lock_stage"] == 2
        assert trade["exit_policy_name"] == "SCALP | stage_2_locked"


# ── MFE / MAE logic simulation ────────────────────────────────────────────────

class TestMFEMAETracking:
    """Simulate the per-bar logic that will run inside market_scanner._update_open_positions."""

    def _simulate_bars(self, entry: float, direction: str, close_prices: list[float]):
        """Run the MFE/MAE update logic over a sequence of bar closes.

        Returns (max_unrealized_pct, min_unrealized_pct).
        """
        max_unrealized = 0.0
        min_unrealized = 0.0
        for close in close_prices:
            if direction == "BUY":
                pct = (close - entry) / entry * 100.0
            else:
                pct = (entry - close) / entry * 100.0
            if pct > max_unrealized:
                max_unrealized = pct
            if pct < min_unrealized:
                min_unrealized = pct
        return max_unrealized, min_unrealized

    def test_long_mfe_increases_on_new_high(self):
        mfe, _ = self._simulate_bars(100.0, "BUY", [101.0, 102.0, 101.5])
        assert mfe == pytest.approx(2.0)

    def test_long_mae_deepens_on_new_low(self):
        _, mae = self._simulate_bars(100.0, "BUY", [99.0, 98.0, 99.5])
        assert mae == pytest.approx(-2.0)

    def test_short_mfe_on_price_falling(self):
        mfe, _ = self._simulate_bars(100.0, "SELL", [99.0, 98.0, 98.5])
        assert mfe == pytest.approx(2.0)

    def test_short_mae_on_price_rising(self):
        _, mae = self._simulate_bars(100.0, "SELL", [101.0, 102.0, 101.0])
        assert mae == pytest.approx(-2.0)

    def test_mfe_never_decreases(self):
        mfe_vals = []
        cur_max = 0.0
        for close in [101.0, 102.0, 101.0, 100.5]:
            pct = (close - 100.0) / 100.0 * 100.0
            cur_max = max(cur_max, pct)
            mfe_vals.append(cur_max)
        assert mfe_vals == sorted(mfe_vals)   # monotonically non-decreasing

    def test_mae_never_improves(self):
        mae_vals = []
        cur_min = 0.0
        for close in [99.0, 98.0, 99.0, 99.5]:
            pct = (close - 100.0) / 100.0 * 100.0
            cur_min = min(cur_min, pct)
            mae_vals.append(cur_min)
        # Should be monotonically non-increasing (each value ≤ previous)
        for i in range(1, len(mae_vals)):
            assert mae_vals[i] <= mae_vals[i - 1]
