"""Phase 14 tests — Live Suitability Activation.

Covers:
    A. SuitabilityRating enum methods
    B. SkipReason / RuleSource constants
    C. SuitabilityContext dataclass + to_dict()
    D. ModeActivationState enum
    E. LiveActivationDecision dataclass + to_trace_dict()
    F. DB schema migration (Phase 14 tables + signal audit columns)
    G. DB helpers (save/get profile snapshots, rules, promoted proposals)
    H. Config knobs present and correctly typed
    I. SuitabilityResolver — default (fail-open) path
    J. SuitabilityResolver — snapshot rule match (BLOCKED)
    K. SuitabilityResolver — snapshot rule match (MEDIUM: threshold_delta + score_penalty)
    L. SuitabilityResolver — promoted proposal fallback when no active snapshot
    M. SuitabilityResolver — most-specific rule wins (specificity ordering)
    N. SuitabilityResolver — SUITABILITY_GATING_ENABLED=False bypasses gating
    O. SuitabilityResolver — resolver never raises (fail-open on DB error)
    P. regime_adapter.check_regime_entry_filter extra_threshold_delta
    Q. Signal types include Phase 14 fields with safe defaults
    R. ProfileMaterializer — build snapshot from promoted proposals
    S. ProfileMaterializer — activate_snapshot marks others inactive
    T. signal_analytics Phase 14 functions
    U. SuitabilityResolver.reload() clears cache
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level DB fixture
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated SQLite DB with the full Phase 14 schema for the current test."""
    import src.data.db as db_mod
    db_file = str(tmp_path / "p14.db")
    monkeypatch.setattr(db_mod, "_sqlite_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "_get_supabase", lambda: None)
    db_mod.init_db()
    return db_mod


def _raw_conn(db_mod) -> sqlite3.Connection:
    """Open a raw sqlite3 connection to the test DB."""
    conn = sqlite3.connect(db_mod._sqlite_path())
    conn.row_factory = sqlite3.Row
    return conn


def _insert_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str | None = None,
    strategy_mode: str = "SCALP",
    macro_regime: str | None = "TRENDING",
    regime_label: str | None = None,
    asset: str | None = None,
    suitability_score: float | None = 0.8,
    approval_status: str = "promoted",
    reason_summary: str = "test proposal",
    threshold_delta: float = 0.0,
    score_penalty: float = 0.0,
    block_entry: int = 0,
) -> str:
    pid = proposal_id or str(uuid.uuid4())
    conn.execute(
        """
        INSERT OR IGNORE INTO optimization_proposals
            (proposal_id, proposal_type, strategy_mode, macro_regime, asset,
             reason_summary, approval_status)
        VALUES (?, 'suitability_test', ?, ?, ?, ?, ?)
        """,
        (pid, strategy_mode, macro_regime, asset, reason_summary, approval_status),
    )
    # Update optional Phase-14 columns if they exist
    for col, val in [
        ("regime_label",     regime_label),
        ("suitability_score", suitability_score),
        ("threshold_delta",   threshold_delta),
        ("score_penalty",     score_penalty),
        ("block_entry",       block_entry),
    ]:
        try:
            conn.execute(
                f"UPDATE optimization_proposals SET {col}=? WHERE proposal_id=?",
                (val, pid),
            )
        except sqlite3.OperationalError:
            pass  # column not yet in schema — safe to skip
    conn.commit()
    return pid


def _insert_snapshot(conn: sqlite3.Connection, *, is_active: int = 1) -> str:
    sid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO active_profile_snapshots
            (snapshot_id, profile_name, is_active, created_at, activated_at)
        VALUES (?, 'test_profile', ?, datetime('now'), datetime('now'))
        """,
        (sid, is_active),
    )
    conn.commit()
    return sid


def _insert_rule(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    macro_regime: str | None = "TRENDING",
    regime_label: str | None = None,
    strategy_mode: str | None = "SCALP",
    asset: str | None = None,
    suitability_rating: str = "HIGH",
    threshold_delta: float = 0.0,
    score_penalty: float = 0.0,
    block_entry: int = 0,
    mode_activation_state: str = "ACTIVE",
) -> str:
    rid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO active_profile_rules
            (rule_id, snapshot_id, macro_regime, regime_label, strategy_mode, asset,
             suitability_rating, threshold_delta, score_penalty, block_entry, mode_activation_state)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (rid, snapshot_id, macro_regime, regime_label, strategy_mode, asset,
         suitability_rating, threshold_delta, score_penalty, block_entry, mode_activation_state),
    )
    conn.commit()
    return rid


def _make_sig(
    *,
    asset: str = "BTC/USD",
    strategy_mode: str = "SCALP",
    score_total: float = 55.0,
) -> Any:
    """Return a minimal BuySignalResult-like object."""
    from src.signals.types import BuySignalResult
    sig = BuySignalResult(
        asset=asset,
        timeframe="5m",
        strategy_mode=strategy_mode,
        score_total=score_total,
        entry_price=100.0,
        stop_loss=98.0,
    )
    return sig


def _make_regime_ctx(
    *,
    macro_regime: str = "TRENDING",
    regime_label: str = "TRENDING_HIGH_VOL",
    confidence: float = 0.8,
) -> Any:
    """Return a minimal regime context mock."""
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.macro_regime.value = macro_regime
    ctx.macro_regime.__str__ = lambda s: macro_regime
    ctx.regime_label.value = regime_label
    ctx.confidence_score = confidence
    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# A. SuitabilityRating enum
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuitabilityRating:
    def test_allows_entry_blocked(self):
        from src.signals.suitability_types import SuitabilityRating
        assert SuitabilityRating.BLOCKED.allows_entry() is False

    def test_allows_entry_all_others(self):
        from src.signals.suitability_types import SuitabilityRating
        for r in (SuitabilityRating.HIGH, SuitabilityRating.MEDIUM,
                  SuitabilityRating.LOW, SuitabilityRating.UNKNOWN):
            assert r.allows_entry() is True

    def test_friction_level_ordering(self):
        from src.signals.suitability_types import SuitabilityRating
        assert SuitabilityRating.HIGH.friction_level()    == 0
        assert SuitabilityRating.MEDIUM.friction_level()  == 2
        assert SuitabilityRating.LOW.friction_level()     == 3
        assert SuitabilityRating.BLOCKED.friction_level() == 4
        assert SuitabilityRating.UNKNOWN.friction_level() == 0

    def test_str_value_preserved(self):
        from src.signals.suitability_types import SuitabilityRating
        assert SuitabilityRating.HIGH.value == "HIGH"
        assert SuitabilityRating.MEDIUM.value == "MEDIUM"


# ═══════════════════════════════════════════════════════════════════════════════
# B. SkipReason / RuleSource
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkipReasonRuleSource:
    def test_all_skip_reason_constants_are_strings(self):
        from src.signals.suitability_types import SkipReason
        for attr in dir(SkipReason):
            if not attr.startswith("_"):
                val = getattr(SkipReason, attr)
                assert isinstance(val, str), f"SkipReason.{attr} is not str"

    def test_rule_source_constants(self):
        from src.signals.suitability_types import RuleSource
        assert RuleSource.ACTIVE_PROFILE_SNAPSHOT == "active_profile_snapshot"
        assert RuleSource.PROMOTED_PROPOSAL       == "promoted_proposal"
        assert RuleSource.DEFAULT_SYSTEM          == "default_system"


# ═══════════════════════════════════════════════════════════════════════════════
# C. SuitabilityContext
# ═══════════════════════════════════════════════════════════════════════════════

class TestSuitabilityContext:
    def test_to_dict_keys(self):
        from src.signals.suitability_types import SuitabilityContext, SuitabilityRating
        ctx = SuitabilityContext(
            strategy_mode="SCALP",
            macro_regime="TRENDING",
            regime_label="TRENDING_HIGH_VOL",
            suitability_rating=SuitabilityRating.HIGH,
            suitability_score=0.85,
            supporting_reason="test",
            source_summary="snapshot",
        )
        d = ctx.to_dict()
        assert d["strategy_mode"] == "SCALP"
        assert d["macro_regime"] == "TRENDING"
        assert d["suitability_rating"] == "HIGH"
        assert d["suitability_score"] == 0.85

    def test_defaults(self):
        from src.signals.suitability_types import SuitabilityContext, SuitabilityRating
        ctx = SuitabilityContext()
        assert ctx.suitability_rating == SuitabilityRating.UNKNOWN
        assert ctx.macro_regime is None


# ═══════════════════════════════════════════════════════════════════════════════
# D. ModeActivationState
# ═══════════════════════════════════════════════════════════════════════════════

class TestModeActivationState:
    def test_values(self):
        from src.signals.suitability_types import ModeActivationState
        assert ModeActivationState.ACTIVE.value    == "ACTIVE"
        assert ModeActivationState.PENALIZED.value == "PENALIZED"
        assert ModeActivationState.BLOCKED.value   == "BLOCKED"

    def test_str_enum(self):
        from src.signals.suitability_types import ModeActivationState
        assert ModeActivationState.ACTIVE.value == "ACTIVE"


# ═══════════════════════════════════════════════════════════════════════════════
# E. LiveActivationDecision
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveActivationDecision:
    def test_default_is_allowed(self):
        from src.signals.suitability_types import LiveActivationDecision
        dec = LiveActivationDecision()
        assert dec.allowed is True
        assert dec.threshold_delta == 0.0
        assert dec.score_penalty == 0.0

    def test_to_trace_dict_shape(self):
        from src.signals.suitability_types import (
            LiveActivationDecision, ModeActivationState,
            RuleSource, SuitabilityContext, SuitabilityRating,
        )
        ctx = SuitabilityContext(suitability_rating=SuitabilityRating.MEDIUM)
        dec = LiveActivationDecision(
            allowed=True,
            suitability_context=ctx,
            mode_activation_state=ModeActivationState.PENALIZED,
            threshold_delta=5.0,
            score_penalty=4.0,
        )
        d = dec.to_trace_dict()
        assert d["suitability_rating"] == "MEDIUM"
        assert d["threshold_delta"] == 5.0
        assert d["score_penalty"] == 4.0
        assert d["mode_activation_state"] == "PENALIZED"

    def test_to_trace_dict_without_context(self):
        from src.signals.suitability_types import LiveActivationDecision
        dec = LiveActivationDecision()
        d = dec.to_trace_dict()
        assert d["suitability_rating"] == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# F. DB schema migration
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBSchemaMigration:
    def test_phase14_tables_exist(self, db):
        conn = _raw_conn(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "active_profile_snapshots" in tables
        assert "active_profile_rules" in tables
        conn.close()

    def test_signal_audit_columns_exist(self, db):
        conn = _raw_conn(db)
        for table in ("buy_signals", "sell_signals"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col in (
                "macro_regime", "regime_label", "suitability_rating",
                "suitability_score", "suitability_reason",
                "suitability_source_summary", "skip_reason_code",
                "decision_trace_json", "active_profile_snapshot_id",
            ):
                assert col in cols, f"{table}.{col} missing"
        conn.close()

    def test_proposals_table_has_extra_columns(self, db):
        conn = _raw_conn(db)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(optimization_proposals)"
        ).fetchall()}
        assert "proposal_id" in cols
        conn.close()

    def test_migration_is_idempotent(self, db):
        """Calling init_db twice must not raise."""
        db.init_db()  # second call


# ═══════════════════════════════════════════════════════════════════════════════
# G. DB helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBHelpers:
    def test_save_and_get_active_profile_snapshot(self, db):
        sid = str(uuid.uuid4())
        db.save_profile_snapshot({
            "snapshot_id":  sid,
            "profile_name": "test",
            "is_active":    1,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        })
        snap = db.get_active_profile_snapshot()
        assert snap is not None
        assert snap["snapshot_id"] == sid

    def test_save_and_get_profile_rules(self, db):
        sid = str(uuid.uuid4())
        db.save_profile_snapshot({"snapshot_id": sid, "profile_name": "t", "is_active": 1})
        rid = str(uuid.uuid4())
        db.save_profile_rule({
            "rule_id":               rid,
            "snapshot_id":           sid,
            "suitability_rating":    "HIGH",
            "mode_activation_state": "ACTIVE",
            "threshold_delta":       0.0,
            "score_penalty":         0.0,
            "block_entry":           0,
        })
        rules = db.get_active_profile_rules(sid)
        assert len(rules) == 1
        assert rules[0]["rule_id"] == rid

    def test_get_promoted_proposals(self, db):
        conn = _raw_conn(db)
        _insert_proposal(conn, approval_status="promoted")
        _insert_proposal(conn, approval_status="draft")      # must be excluded
        conn.close()
        promoted = db.get_promoted_proposals_for_fallback()
        assert len(promoted) == 1
        assert promoted[0]["approval_status"] == "promoted"

    def test_no_active_snapshot_returns_none(self, db):
        assert db.get_active_profile_snapshot() is None


# ═══════════════════════════════════════════════════════════════════════════════
# H. Config knobs
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigKnobs:
    def test_all_knobs_present(self):
        import src.config as cfg
        assert isinstance(cfg.SUITABILITY_GATING_ENABLED, bool)
        assert isinstance(cfg.SUITABILITY_THRESHOLD_RAISE_ENABLED, bool)
        assert isinstance(cfg.SUITABILITY_SCORE_PENALTY_ENABLED, bool)
        assert isinstance(cfg.SUITABILITY_MEDIUM_THRESHOLD_DELTA, float)
        assert isinstance(cfg.SUITABILITY_MEDIUM_SCORE_PENALTY, float)
        assert isinstance(cfg.SUITABILITY_LOW_THRESHOLD_DELTA, float)
        assert isinstance(cfg.SUITABILITY_LOW_SCORE_PENALTY, float)

    def test_default_values(self):
        import src.config as cfg
        assert cfg.SUITABILITY_GATING_ENABLED is True
        assert cfg.SUITABILITY_MEDIUM_THRESHOLD_DELTA == 5.0
        assert cfg.SUITABILITY_MEDIUM_SCORE_PENALTY   == 4.0
        assert cfg.SUITABILITY_LOW_THRESHOLD_DELTA    == 10.0
        assert cfg.SUITABILITY_LOW_SCORE_PENALTY      == 8.0


# ═══════════════════════════════════════════════════════════════════════════════
# I. SuitabilityResolver — default fail-open path
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverDefault:
    def test_default_decision_allowed(self, db):
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(), None)
        assert dec.allowed is True
        assert dec.threshold_delta == 0.0
        assert dec.score_penalty   == 0.0

    def test_gating_disabled_always_returns_allowed(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", False)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid, suitability_rating="BLOCKED", block_entry=1)
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(), _make_regime_ctx())
        assert dec.allowed is True


# ═══════════════════════════════════════════════════════════════════════════════
# J. SuitabilityResolver — BLOCKED rule
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverBlocked:
    def test_blocked_rule_blocks_entry(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid,
                     macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="BLOCKED", block_entry=1,
                     mode_activation_state="BLOCKED")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(strategy_mode="SCALP"),
                               _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.allowed is False
        assert dec.skip_reason_code == "blocked_by_suitability"
        assert "blocked_by_suitability" in dec.rejection_reason

    def test_blocked_state_makes_allowed_false(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid,
                     macro_regime="RANGING", strategy_mode="SWING",
                     suitability_rating="LOW", mode_activation_state="BLOCKED")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(strategy_mode="SWING"),
                               _make_regime_ctx(macro_regime="RANGING"))
        assert dec.allowed is False


# ═══════════════════════════════════════════════════════════════════════════════
# K. SuitabilityResolver — MEDIUM friction
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverMediumFriction:
    def test_medium_applies_default_deltas(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   True)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_THRESHOLD_DELTA",  5.0)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",    4.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid,
                     macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="MEDIUM")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(strategy_mode="SCALP"),
                               _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.allowed is True
        assert dec.threshold_delta == pytest.approx(5.0)
        assert dec.score_penalty   == pytest.approx(4.0)
        assert dec.mode_activation_state.value == "PENALIZED"

    def test_threshold_raise_disabled_zero_delta(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", False)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   True)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_THRESHOLD_DELTA",  5.0)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",    4.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid,
                     macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="MEDIUM")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        dec = SuitabilityResolver().resolve(_make_sig(), _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.threshold_delta == pytest.approx(0.0)
        assert dec.score_penalty   == pytest.approx(4.0)

    def test_score_penalty_disabled_zero_penalty(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   False)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_THRESHOLD_DELTA",  5.0)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",    4.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid, macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="MEDIUM")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        dec = SuitabilityResolver().resolve(_make_sig(), _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.threshold_delta == pytest.approx(5.0)
        assert dec.score_penalty   == pytest.approx(0.0)

    def test_low_rating_applies_low_deltas(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   True)
        monkeypatch.setattr(cfg, "SUITABILITY_LOW_THRESHOLD_DELTA",     10.0)
        monkeypatch.setattr(cfg, "SUITABILITY_LOW_SCORE_PENALTY",       8.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid, macro_regime="RANGING", strategy_mode="SWING",
                     suitability_rating="LOW")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        dec = SuitabilityResolver().resolve(_make_sig(strategy_mode="SWING"),
                                            _make_regime_ctx(macro_regime="RANGING"))
        assert dec.threshold_delta == pytest.approx(10.0)
        assert dec.score_penalty   == pytest.approx(8.0)


# ═══════════════════════════════════════════════════════════════════════════════
# L. SuitabilityResolver — promoted proposal fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverProposalFallback:
    def test_uses_promoted_proposal_when_no_snapshot(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",        True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",  4.0)
        conn = _raw_conn(db)
        # No active snapshot; insert promoted proposal with MEDIUM suitability
        _insert_proposal(conn,
                         strategy_mode="SCALP",
                         macro_regime="TRENDING",
                         suitability_score=0.6,
                         approval_status="promoted")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(strategy_mode="SCALP"),
                               _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.allowed is True
        assert dec.score_penalty   == pytest.approx(4.0)
        assert dec.applied_rule_source == "promoted_proposal"

    def test_draft_proposal_not_used_as_fallback(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        conn = _raw_conn(db)
        _insert_proposal(conn, strategy_mode="SCALP", macro_regime="TRENDING",
                         suitability_score=0.1, approval_status="draft")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(strategy_mode="SCALP"),
                               _make_regime_ctx(macro_regime="TRENDING"))
        # draft proposal excluded → falls to default
        assert dec.allowed is True
        assert dec.applied_rule_source == "default_system"


# ═══════════════════════════════════════════════════════════════════════════════
# M. SuitabilityResolver — most-specific rule wins
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverSpecificity:
    def test_asset_specific_rule_beats_mode_only(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   True)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_THRESHOLD_DELTA",  5.0)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",    4.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        # Broad rule — affects all SCALP in TRENDING
        _insert_rule(conn, sid, macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="MEDIUM")
        # Specific rule — only for BTC/USD SCALP in TRENDING (should win)
        _insert_rule(conn, sid, macro_regime="TRENDING", strategy_mode="SCALP",
                     asset="BTC/USD", suitability_rating="HIGH")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(asset="BTC/USD", strategy_mode="SCALP"),
                               _make_regime_ctx(macro_regime="TRENDING"))
        # Asset-specific HIGH rule should win → no friction
        assert dec.score_penalty   == pytest.approx(0.0)
        assert dec.threshold_delta == pytest.approx(0.0)

    def test_regime_label_beats_macro_only(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED",          True)
        monkeypatch.setattr(cfg, "SUITABILITY_THRESHOLD_RAISE_ENABLED", True)
        monkeypatch.setattr(cfg, "SUITABILITY_SCORE_PENALTY_ENABLED",   True)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_THRESHOLD_DELTA",  5.0)
        monkeypatch.setattr(cfg, "SUITABILITY_MEDIUM_SCORE_PENALTY",    4.0)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        # Broad: TRENDING / SCALP → MEDIUM
        _insert_rule(conn, sid, macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="MEDIUM")
        # Narrow: TRENDING_HIGH_VOL / SCALP → HIGH (no friction)
        _insert_rule(conn, sid, macro_regime="TRENDING",
                     regime_label="TRENDING_HIGH_VOL", strategy_mode="SCALP",
                     suitability_rating="HIGH")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        ctx = _make_regime_ctx(macro_regime="TRENDING", regime_label="TRENDING_HIGH_VOL")
        dec = resolver.resolve(_make_sig(strategy_mode="SCALP"), ctx)
        # Specific regime_label rule should win → HIGH → no friction
        assert dec.score_penalty   == pytest.approx(0.0)
        assert dec.threshold_delta == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# N. Gating disabled
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverGatingDisabled:
    def test_gating_off_returns_default_always(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", False)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid, suitability_rating="BLOCKED", block_entry=1,
                     mode_activation_state="BLOCKED")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(), _make_regime_ctx())
        assert dec.allowed is True
        assert dec.applied_rule_source == "default_system"


# ═══════════════════════════════════════════════════════════════════════════════
# O. Resolver never raises
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverFailOpen:
    def test_db_error_returns_default(self, tmp_path, monkeypatch):
        """Pointing _sqlite_path to a nonexistent dir must still fail-open."""
        import src.data.db as db_mod
        import src.config as cfg
        monkeypatch.setattr(db_mod, "_sqlite_path",
                            lambda: str(tmp_path / "nodir" / "sub" / "db.db"))
        monkeypatch.setattr(db_mod, "_get_supabase", lambda: None)
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        dec = resolver.resolve(_make_sig(), None)
        assert dec.allowed is True

    def test_bad_regime_ctx_does_not_raise(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        bad_ctx = MagicMock()
        bad_ctx.macro_regime = MagicMock(side_effect=RuntimeError("boom"))
        dec = resolver.resolve(_make_sig(), bad_ctx)
        assert isinstance(dec.allowed, bool)


# ═══════════════════════════════════════════════════════════════════════════════
# P. regime_adapter.check_regime_entry_filter extra_threshold_delta
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeAdapterThresholdDelta:
    def _make_ctx(self):
        from src.signals.regime_types import MacroRegime
        ctx = MagicMock()
        ctx.macro_labels.return_value = frozenset([MacroRegime.TRENDING])
        ctx.confidence_score    = 0.9
        ctx.is_confident.return_value = True
        ctx.regime_entry_allowed = None
        ctx.regime_entry_reason  = None
        return ctx

    def test_no_delta_passes_above_base(self, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "REGIME_ENTRY_FILTER_ENABLED",     True)
        monkeypatch.setattr(cfg, "REGIME_ENTRY_MIN_SCORE_TRENDING", 30.0)
        from src.signals.regime_adapter import check_regime_entry_filter
        sig = _make_sig(score_total=35.0)
        ok, _ = check_regime_entry_filter(sig, self._make_ctx(), extra_threshold_delta=0.0)
        assert ok is True

    def test_extra_delta_raises_bar_and_rejects(self, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "REGIME_ENTRY_FILTER_ENABLED",     True)
        monkeypatch.setattr(cfg, "REGIME_ENTRY_MIN_SCORE_TRENDING", 30.0)
        from src.signals.regime_adapter import check_regime_entry_filter
        # score=35, base=30, delta=10 → effective=40 → reject
        sig = _make_sig(score_total=35.0)
        ok, reason = check_regime_entry_filter(sig, self._make_ctx(), extra_threshold_delta=10.0)
        assert ok is False
        assert "suitability_delta=10.0" in reason

    def test_extra_delta_zero_still_allowed(self, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "REGIME_ENTRY_FILTER_ENABLED",     True)
        monkeypatch.setattr(cfg, "REGIME_ENTRY_MIN_SCORE_TRENDING", 30.0)
        from src.signals.regime_adapter import check_regime_entry_filter
        sig = _make_sig(score_total=40.0)
        ok, _ = check_regime_entry_filter(sig, self._make_ctx(), extra_threshold_delta=0.0)
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# Q. Signal types Phase 14 fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalTypesPhase14Fields:
    def test_buy_signal_has_phase14_fields(self):
        from src.signals.types import BuySignalResult
        sig = BuySignalResult()
        assert hasattr(sig, "suitability_context")
        assert hasattr(sig, "suitability_rating")
        assert hasattr(sig, "suitability_score")
        assert hasattr(sig, "suitability_reason")
        assert hasattr(sig, "suitability_source_summary")
        assert hasattr(sig, "skip_reason_code")
        assert hasattr(sig, "decision_trace_json")
        assert hasattr(sig, "active_profile_snapshot_id")
        assert hasattr(sig, "live_activation_decision")
        assert hasattr(sig, "macro_regime")
        assert hasattr(sig, "regime_label")

    def test_sell_signal_has_phase14_fields(self):
        from src.signals.types import SellSignalResult
        sig = SellSignalResult()
        assert hasattr(sig, "suitability_rating")
        assert hasattr(sig, "skip_reason_code")
        assert hasattr(sig, "live_activation_decision")

    def test_defaults_are_safe(self):
        from src.signals.types import BuySignalResult
        sig = BuySignalResult()
        assert sig.suitability_context is None
        assert sig.suitability_rating  is None
        assert sig.suitability_score   is None
        assert sig.suitability_reason  == ""
        assert sig.skip_reason_code    == ""
        assert sig.macro_regime        is None
        assert sig.regime_label        is None


# ═══════════════════════════════════════════════════════════════════════════════
# R. ProfileMaterializer — build snapshot
# ═══════════════════════════════════════════════════════════════════════════════

class TestProfileMaterializer:
    def test_build_from_promoted_proposals(self, db):
        conn = _raw_conn(db)
        _insert_proposal(conn, approval_status="promoted", suitability_score=0.8)
        _insert_proposal(conn, approval_status="promoted", suitability_score=0.5)
        conn.close()
        from src.tools.profile_materializer import build_snapshot_from_promoted_proposals
        sid = build_snapshot_from_promoted_proposals()
        assert sid is not None
        conn2 = _raw_conn(db)
        row = conn2.execute(
            "SELECT * FROM active_profile_snapshots WHERE snapshot_id=?", (sid,)
        ).fetchone()
        assert row is not None
        assert row["profile_name"] == "auto_promoted"
        rules = conn2.execute(
            "SELECT * FROM active_profile_rules WHERE snapshot_id=?", (sid,)
        ).fetchall()
        assert len(rules) == 2
        conn2.close()

    def test_no_promoted_proposals_returns_none(self, db):
        from src.tools.profile_materializer import build_snapshot_from_promoted_proposals
        assert build_snapshot_from_promoted_proposals() is None

    def test_activate_snapshot_sets_active(self, db):
        from src.tools.profile_materializer import (
            activate_snapshot,
            build_snapshot_from_promoted_proposals,
        )
        conn = _raw_conn(db)
        _insert_proposal(conn, approval_status="promoted")
        conn.close()
        sid = build_snapshot_from_promoted_proposals()
        assert sid is not None
        ok = activate_snapshot(sid)
        assert ok is True
        conn2 = _raw_conn(db)
        row = conn2.execute(
            "SELECT is_active FROM active_profile_snapshots WHERE snapshot_id=?", (sid,)
        ).fetchone()
        assert row["is_active"] == 1
        conn2.close()

    def test_activate_deactivates_previous(self, db):
        from src.tools.profile_materializer import activate_snapshot
        sid1 = str(uuid.uuid4())
        sid2 = str(uuid.uuid4())
        db.save_profile_snapshot({"snapshot_id": sid1, "profile_name": "a", "is_active": 1})
        db.save_profile_snapshot({"snapshot_id": sid2, "profile_name": "b", "is_active": 0})
        activate_snapshot(sid2)
        conn = _raw_conn(db)
        r1 = conn.execute(
            "SELECT is_active FROM active_profile_snapshots WHERE snapshot_id=?", (sid1,)
        ).fetchone()
        r2 = conn.execute(
            "SELECT is_active FROM active_profile_snapshots WHERE snapshot_id=?", (sid2,)
        ).fetchone()
        assert r1["is_active"] == 0
        assert r2["is_active"] == 1
        conn.close()

    def test_score_to_rating_boundaries(self):
        from src.tools.profile_materializer import _score_to_rating
        assert _score_to_rating(None)  == "HIGH"
        assert _score_to_rating(0.80)  == "HIGH"
        assert _score_to_rating(0.75)  == "HIGH"
        assert _score_to_rating(0.74)  == "MEDIUM"
        assert _score_to_rating(0.50)  == "MEDIUM"
        assert _score_to_rating(0.49)  == "LOW"
        assert _score_to_rating(0.25)  == "LOW"
        assert _score_to_rating(0.24)  == "BLOCKED"
        assert _score_to_rating(0.0)   == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════════════════
# S. SuitabilityResolver.reload()
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverReload:
    def test_reload_clears_cache(self, db):
        from src.signals.suitability_resolver import SuitabilityResolver
        resolver = SuitabilityResolver()
        resolver._load_snapshot()
        assert resolver._snapshot_loaded is True
        resolver.reload()
        assert resolver._snapshot_loaded is False
        assert resolver._snapshot_id is None
        assert resolver._rules == []
        assert resolver._promoted_proposals is None


# ═══════════════════════════════════════════════════════════════════════════════
# T. signal_analytics Phase 14 functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalAnalyticsPhase14:
    def _seed(self, db):
        conn = _raw_conn(db)
        conn.execute(
            """INSERT INTO buy_signals
               (asset, timeframe, timestamp, is_valid, points,
                alligator_pt, stochastic_pt, vortex_pt,
                macro_regime, suitability_rating, skip_reason_code)
               VALUES
               ('BTC/USD','5m','2024-01-01T00:00:00',1,2,1,1,0,
                'TRENDING','MEDIUM','score_penalty_applied'),
               ('ETH/USD','5m','2024-01-01T00:01:00',1,3,1,1,1,
                'RANGING','BLOCKED','blocked_by_suitability'),
               ('BTC/USD','5m','2024-01-01T00:02:00',1,3,1,1,1,
                'TRENDING','HIGH',NULL)
            """
        )
        conn.commit()
        conn.close()
        return db._sqlite_path()

    def test_skip_reason_frequency(self, db):
        db_path = self._seed(db)
        from src.signals.signal_analytics import skip_reason_frequency
        result = skip_reason_frequency(db_path, "BUY")
        codes = [r["skip_reason_code"] for r in result]
        assert "blocked_by_suitability" in codes
        assert "score_penalty_applied"  in codes

    def test_suitability_rating_distribution(self, db):
        db_path = self._seed(db)
        from src.signals.signal_analytics import suitability_rating_distribution
        dist = suitability_rating_distribution(db_path, "BUY")
        assert dist.get("HIGH",    0) >= 1
        assert dist.get("MEDIUM",  0) >= 1
        assert dist.get("BLOCKED", 0) >= 1

    def test_prevented_by_suitability_count(self, db):
        db_path = self._seed(db)
        from src.signals.signal_analytics import prevented_by_suitability_count
        assert prevented_by_suitability_count(db_path, "BUY") == 1

    def test_skipped_by_regime_summary(self, db):
        db_path = self._seed(db)
        from src.signals.signal_analytics import skipped_by_regime_summary
        rows = skipped_by_regime_summary(db_path, "BUY")
        assert len(rows) >= 1
        regimes = [r["macro_regime"] for r in rows]
        assert "TRENDING" in regimes or "RANGING" in regimes

    def test_empty_db_returns_empty(self, db):
        db_path = db._sqlite_path()
        from src.signals.signal_analytics import (
            skip_reason_frequency, suitability_rating_distribution,
            prevented_by_suitability_count, skipped_by_regime_summary,
        )
        assert skip_reason_frequency(db_path,           "BUY") == []
        assert suitability_rating_distribution(db_path, "BUY") == {}
        assert prevented_by_suitability_count(db_path,  "BUY") == 0
        assert skipped_by_regime_summary(db_path,       "BUY") == []


# ═══════════════════════════════════════════════════════════════════════════════
# U. High-suitability rating — zero friction (HIGH / UNKNOWN passthrough)
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolverHighUnknown:
    def test_high_rating_no_friction(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        conn = _raw_conn(db)
        sid = _insert_snapshot(conn)
        _insert_rule(conn, sid, macro_regime="TRENDING", strategy_mode="SCALP",
                     suitability_rating="HIGH")
        conn.close()
        from src.signals.suitability_resolver import SuitabilityResolver
        dec = SuitabilityResolver().resolve(_make_sig(), _make_regime_ctx(macro_regime="TRENDING"))
        assert dec.allowed is True
        assert dec.threshold_delta == pytest.approx(0.0)
        assert dec.score_penalty   == pytest.approx(0.0)
        assert dec.mode_activation_state.value == "ACTIVE"

    def test_unknown_rating_no_friction(self, db, monkeypatch):
        import src.config as cfg
        monkeypatch.setattr(cfg, "SUITABILITY_GATING_ENABLED", True)
        # No rules in DB → falls all the way to default
        from src.signals.suitability_resolver import SuitabilityResolver
        dec = SuitabilityResolver().resolve(_make_sig(), _make_regime_ctx())
        assert dec.allowed is True
        assert dec.threshold_delta == pytest.approx(0.0)
        assert dec.score_penalty   == pytest.approx(0.0)
