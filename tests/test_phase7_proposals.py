"""Phase 7 — Proposal Engine & MFE/MAE Report tests.

Tests cover:
  A. DB layer  — optimization_proposals table, save/get/transition, state machine
  B. ProposalRecord dataclass and to_dict() serialisation
  C. ProposalType and ProposalStatus enums
  D. Auto-promotion guardrail (draft → promoted must raise)
  E. generate_proposals() on empty DB returns [] gracefully
  F. analyze_mfe_mae() flag logic (3 categories)
  G. Proposal reporter serialisers (JSON, Markdown) produce valid output
  H. ProposalRecord field validation and defaults
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Shared DB fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """Patch SQLITE_PATH to an isolated temp file; initialise schema."""
    db_file = str(tmp_path / "test_phase7.db")
    monkeypatch.setenv("SQLITE_PATH", db_file)

    import src.data.db as db_mod
    monkeypatch.setattr(db_mod, "SQLITE_PATH", db_file)
    db_mod._sb_client = None
    db_mod.init_db()
    return db_mod


# ─────────────────────────────────────────────────────────────────────────────
# Class 1 — Enum values
# ─────────────────────────────────────────────────────────────────────────────

class TestProposalEnums:
    def test_proposal_type_has_all_15_members(self):
        from src.tools.proposal_engine import ProposalType
        assert len(ProposalType) == 15

    def test_proposal_type_values(self):
        from src.tools.proposal_engine import ProposalType
        expected = {
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
            "regime_threshold_change",
            "regime_exit_policy_change",
            "regime_fade_requirement_change",
            "regime_ml_veto_change",
            "regime_ai_veto_change",
        }
        assert {m.value for m in ProposalType} == expected

    def test_proposal_status_has_all_9_members(self):
        from src.tools.proposal_engine import ProposalStatus
        assert len(ProposalStatus) == 9

    def test_proposal_status_values(self):
        from src.tools.proposal_engine import ProposalStatus
        expected = {
            "draft",
            "backtest_pending",
            "backtest_complete",
            "paper_validation_pending",
            "paper_validation_complete",
            "approved",
            "rejected",
            "promoted",
            "superseded",
        }
        assert {m.value for m in ProposalStatus} == expected

    def test_proposal_type_is_str_enum(self):
        """ProposalType members can be compared directly to str literals."""
        from src.tools.proposal_engine import ProposalType
        assert ProposalType.THRESHOLD_CHANGE == "threshold_change"

    def test_proposal_status_is_str_enum(self):
        from src.tools.proposal_engine import ProposalStatus
        assert ProposalStatus.PROMOTED == "promoted"


# ─────────────────────────────────────────────────────────────────────────────
# Class 2 — ProposalRecord dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestProposalRecord:
    def test_auto_id_generated(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="test")
        assert isinstance(p.proposal_id, str)
        assert len(p.proposal_id) == 36  # UUID-4 canonical form

    def test_two_records_have_distinct_ids(self):
        from src.tools.proposal_engine import ProposalRecord
        p1 = ProposalRecord(proposal_type="threshold_change", reason_summary="a")
        p2 = ProposalRecord(proposal_type="threshold_change", reason_summary="b")
        assert p1.proposal_id != p2.proposal_id

    def test_created_at_is_iso_string(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="ts test")
        # Must be valid ISO 8601 — parse without raising
        from datetime import datetime
        datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))

    def test_default_approval_status_is_draft(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="x")
        assert p.approval_status == "draft"

    def test_to_dict_contains_required_keys(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(
            proposal_type="exit_policy_tightening",
            strategy_mode="SCALP",
            reason_summary="test reason",
            current_value={"avg_capture_ratio": 0.3},
            proposed_value={"action": "tighten"},
        )
        d = p.to_dict()
        for key in (
            "proposal_id", "created_at", "proposal_type", "strategy_mode",
            "asset", "asset_class", "current_value", "proposed_value",
            "reason_summary", "evidence_summary", "evidence_metrics_json",
            "backtest_status", "paper_validation_status", "approval_status",
            "promoted_at", "superseded_by",
        ):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_json_encodes_current_value(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(
            proposal_type="threshold_change",
            reason_summary="json test",
            current_value={"threshold": 70},
        )
        d = p.to_dict()
        decoded = json.loads(d["current_value"])
        assert decoded == {"threshold": 70}

    def test_to_dict_json_encodes_proposed_value(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(
            proposal_type="threshold_change",
            reason_summary="json test",
            proposed_value=75,
        )
        d = p.to_dict()
        assert json.loads(d["proposed_value"]) == 75

    def test_to_dict_json_encodes_evidence_metrics(self):
        from src.tools.proposal_engine import ProposalRecord
        metrics = {"n": 10, "win_rate": 0.6}
        p = ProposalRecord(
            proposal_type="threshold_change",
            reason_summary="evidence test",
            evidence_metrics=metrics,
        )
        d = p.to_dict()
        assert json.loads(d["evidence_metrics_json"]) == metrics

    def test_none_values_stay_none_in_dict(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="none test")
        d = p.to_dict()
        assert d["current_value"] is None
        assert d["proposed_value"] is None
        assert d["evidence_metrics_json"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Class 3 — DB state machine transitions
# ─────────────────────────────────────────────────────────────────────────────

class TestStatusTransitions:
    def _save_draft(self, db_mod, pid: str | None = None) -> str:
        """Insert a minimal draft proposal; return its proposal_id."""
        pid = pid or str(uuid.uuid4())
        db_mod.save_proposal({
            "proposal_id": pid,
            "proposal_type": "threshold_change",
            "reason_summary": "test draft",
            "approval_status": "draft",
        })
        return pid

    def test_draft_to_backtest_pending_allowed(self, _db):
        pid = self._save_draft(_db)
        _db.transition_proposal_status(pid, "backtest_pending")
        rows = _db.get_proposals(status="backtest_pending")
        assert any(r["proposal_id"] == pid for r in rows)

    def test_draft_to_rejected_allowed(self, _db):
        pid = self._save_draft(_db)
        _db.transition_proposal_status(pid, "rejected")
        rows = _db.get_proposals(status="rejected")
        assert any(r["proposal_id"] == pid for r in rows)

    def test_full_happy_path(self, _db):
        """Walk a proposal through the entire approval pipeline."""
        pid = self._save_draft(_db)
        for status in (
            "backtest_pending",
            "backtest_complete",
            "paper_validation_pending",
            "paper_validation_complete",
            "approved",
            "promoted",
        ):
            _db.transition_proposal_status(pid, status)

        rows = _db.get_proposals(status="promoted")
        assert any(r["proposal_id"] == pid for r in rows)

    def test_promoted_proposal_has_promoted_at_set(self, _db):
        """After promotion, promoted_at timestamp must be a non-null string."""
        pid = self._save_draft(_db)
        for status in (
            "backtest_pending", "backtest_complete",
            "paper_validation_pending", "paper_validation_complete",
            "approved", "promoted",
        ):
            _db.transition_proposal_status(pid, status)

        rows = _db.get_proposals(status="promoted")
        row = next(r for r in rows if r["proposal_id"] == pid)
        assert row["promoted_at"] is not None
        assert isinstance(row["promoted_at"], str)

    def test_nonexistent_proposal_raises(self, _db):
        with pytest.raises(ValueError, match="not found"):
            _db.transition_proposal_status("nonexistent-id-xyz", "backtest_pending")

    def test_unknown_new_status_raises(self, _db):
        pid = self._save_draft(_db)
        with pytest.raises(ValueError):
            _db.transition_proposal_status(pid, "flying")

    def test_rejected_is_terminal(self, _db):
        pid = self._save_draft(_db)
        _db.transition_proposal_status(pid, "rejected")
        with pytest.raises(ValueError):
            _db.transition_proposal_status(pid, "draft")


# ─────────────────────────────────────────────────────────────────────────────
# Class 4 — Auto-promotion guardrail (critical safety test)
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoPromotionBlocked:
    def _save_draft(self, db_mod, pid: str | None = None) -> str:
        pid = pid or str(uuid.uuid4())
        db_mod.save_proposal({
            "proposal_id": pid,
            "proposal_type": "threshold_change",
            "reason_summary": "promo test",
            "approval_status": "draft",
        })
        return pid

    def test_draft_cannot_be_directly_promoted(self, _db):
        """CRITICAL: skip-to-promote from draft must be rejected by the state machine."""
        pid = self._save_draft(_db)
        with pytest.raises(ValueError) as exc_info:
            _db.transition_proposal_status(pid, "promoted")
        assert "promoted" in str(exc_info.value).lower() or "draft" in str(exc_info.value).lower()

    def test_backtest_complete_cannot_skip_to_promoted(self, _db):
        pid = self._save_draft(_db)
        _db.transition_proposal_status(pid, "backtest_pending")
        _db.transition_proposal_status(pid, "backtest_complete")
        with pytest.raises(ValueError):
            _db.transition_proposal_status(pid, "promoted")

    def test_approved_can_be_promoted(self, _db):
        """Confirm the correct path works."""
        pid = self._save_draft(_db)
        for status in (
            "backtest_pending", "backtest_complete",
            "paper_validation_pending", "paper_validation_complete",
            "approved",
        ):
            _db.transition_proposal_status(pid, status)
        # Must NOT raise
        _db.transition_proposal_status(pid, "promoted")

    def test_approve_helper_works(self, _db):
        """approve_proposal() helper advances to approved state."""
        from src.tools.proposal_engine import approve_proposal
        pid = self._save_draft(_db)
        for status in (
            "backtest_pending", "backtest_complete",
            "paper_validation_pending", "paper_validation_complete",
        ):
            _db.transition_proposal_status(pid, status)
        # patch SQLITE_PATH inside db module to our temp db
        import src.data.db as db_mod
        original = db_mod.SQLITE_PATH
        db_mod.SQLITE_PATH = _db.SQLITE_PATH
        try:
            approve_proposal(pid)
        finally:
            db_mod.SQLITE_PATH = original
        rows = _db.get_proposals(status="approved")
        assert any(r["proposal_id"] == pid for r in rows)

    def test_generate_proposals_returns_only_drafts(self, tmp_path):
        """All proposals from generate_proposals() must have approval_status='draft'."""
        from src.tools.proposal_engine import generate_proposals
        db_path = tmp_path / "empty.db"
        results = generate_proposals(db_path=str(db_path))
        for p in results:
            assert p.approval_status == "draft"


# ─────────────────────────────────────────────────────────────────────────────
# Class 5 — DB migration idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestDbMigration:
    def test_migrate_proposals_table_idempotent(self, _db):
        """Calling the migration twice must not raise."""
        _db.migrate_add_proposals_table()  # second call — schema already exists
        _db.migrate_add_proposals_table()  # third call

    def test_optimization_proposals_columns_present(self, _db):
        with _db._sqlite_conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(optimization_proposals)")}
        for expected in (
            "proposal_id", "created_at", "proposal_type", "strategy_mode",
            "asset", "asset_class", "current_value", "proposed_value",
            "reason_summary", "evidence_summary", "evidence_metrics_json",
            "backtest_status", "paper_validation_status", "approval_status",
            "promoted_at", "superseded_by",
        ):
            assert expected in cols, f"Missing column: {expected}"


# ─────────────────────────────────────────────────────────────────────────────
# Class 6 — save_proposal / get_proposals roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveAndLoadProposal:
    def test_save_and_retrieve_proposal(self, _db):
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id":      pid,
            "proposal_type":    "exit_policy_tightening",
            "strategy_mode":    "SCALP",
            "reason_summary":   "capture ratio too low",
            "evidence_summary": "n=25 capture=0.32",
            "approval_status":  "draft",
        })
        rows = _db.get_proposals()
        assert any(r["proposal_id"] == pid for r in rows)

    def test_saved_fields_are_preserved(self, _db):
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id":   pid,
            "proposal_type": "ml_veto_change",
            "strategy_mode": "INTERMEDIATE",
            "asset":         "BTCUSDT",
            "reason_summary":"ml veto rate too high",
            "current_value": json.dumps(0.65),
            "proposed_value":json.dumps(0.60),
            "approval_status":"draft",
        })
        rows = _db.get_proposals()
        row  = next(r for r in rows if r["proposal_id"] == pid)
        assert row["proposal_type"] == "ml_veto_change"
        assert row["strategy_mode"] == "INTERMEDIATE"
        assert row["asset"]         == "BTCUSDT"

    def test_invalid_proposal_type_raises(self, _db):
        with pytest.raises(ValueError, match="Unknown proposal_type"):
            _db.save_proposal({
                "proposal_id":   str(uuid.uuid4()),
                "proposal_type": "nonexistent_type",
                "reason_summary":"bad type",
            })

    def test_invalid_approval_status_raises(self, _db):
        with pytest.raises(ValueError, match="Unknown approval_status"):
            _db.save_proposal({
                "proposal_id":   str(uuid.uuid4()),
                "proposal_type": "threshold_change",
                "reason_summary":"bad status",
                "approval_status":"invalid_state",
            })

    def test_get_proposals_summary_counts_correctly(self, _db):
        for _ in range(3):
            _db.save_proposal({
                "proposal_id":   str(uuid.uuid4()),
                "proposal_type": "threshold_change",
                "reason_summary":"count test",
                "approval_status":"draft",
            })
        summary = _db.get_proposals_summary()
        assert summary.get("draft", 0) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# Class 7 — get_proposals filters
# ─────────────────────────────────────────────────────────────────────────────

class TestGetProposalsFilters:
    def _seed(self, db_mod):
        rows = [
            {"proposal_id": str(uuid.uuid4()), "proposal_type": "threshold_change",      "strategy_mode": "SCALP",        "reason_summary": "a", "approval_status": "draft"},
            {"proposal_id": str(uuid.uuid4()), "proposal_type": "exit_policy_tightening","strategy_mode": "INTERMEDIATE", "reason_summary": "b", "approval_status": "backtest_pending"},
            {"proposal_id": str(uuid.uuid4()), "proposal_type": "ml_veto_change",        "strategy_mode": "SWING",        "reason_summary": "c", "approval_status": "draft"},
        ]
        for r in rows:
            db_mod.save_proposal(r)
        return rows

    def test_filter_by_status(self, _db):
        self._seed(_db)
        drafts = _db.get_proposals(status="draft")
        assert all(r["approval_status"] == "draft" for r in drafts)
        assert len(drafts) >= 2

    def test_filter_by_mode(self, _db):
        self._seed(_db)
        scalp = _db.get_proposals(strategy_mode="SCALP")
        assert all(r["strategy_mode"] == "SCALP" for r in scalp)

    def test_filter_by_type(self, _db):
        self._seed(_db)
        ml_rows = _db.get_proposals(proposal_type="ml_veto_change")
        assert all(r["proposal_type"] == "ml_veto_change" for r in ml_rows)

    def test_combined_filter(self, _db):
        self._seed(_db)
        results = _db.get_proposals(status="draft", strategy_mode="SCALP")
        assert all(r["approval_status"] == "draft" and r["strategy_mode"] == "SCALP" for r in results)

    def test_no_matches_returns_empty_list(self, _db):
        result = _db.get_proposals(status="promoted")
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# Class 8 — MFE/MAE report logic
# ─────────────────────────────────────────────────────────────────────────────

class TestMfeMaeReport:
    def _make_trade(self, **kwargs) -> dict:
        defaults = {
            "trade_id":              str(uuid.uuid4()),
            "asset":                 "BTCUSDT",
            "strategy_mode":         "SCALP",
            "pnl_pct":               1.0,
            "max_unrealized_profit": 1.0,
            "min_unrealized_profit": -0.5,
            "was_protected_profit":  False,
            "break_even_armed":      False,
            "profit_lock_stage":     0,
        }
        defaults.update(kwargs)
        return defaults

    # Flag 1 — high MFE / poor PnL
    def test_flag1_mfe_2_pnl_0_3_is_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(max_unrealized_profit=2.0, pnl_pct=0.3)
        result = analyze_mfe_mae([trade])
        assert len(result["high_mfe_poor_pnl"]) == 1

    def test_flag1_mfe_2_pnl_0_9_is_not_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(max_unrealized_profit=2.0, pnl_pct=0.9)
        result = analyze_mfe_mae([trade])
        assert len(result["high_mfe_poor_pnl"]) == 0

    def test_flag1_low_mfe_not_flagged_even_if_poor_pnl(self):
        """MFE below threshold → not in flag-1 bucket."""
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(max_unrealized_profit=0.5, pnl_pct=0.1)
        result = analyze_mfe_mae([trade])
        assert len(result["high_mfe_poor_pnl"]) == 0

    def test_flag1_capture_ratio_attached(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(max_unrealized_profit=2.0, pnl_pct=0.4)
        result = analyze_mfe_mae([trade])
        flagged = result["high_mfe_poor_pnl"]
        assert len(flagged) == 1
        assert "_capture_ratio" in flagged[0]
        assert abs(flagged[0]["_capture_ratio"] - 0.2) < 1e-6

    # Flag 2 — giveback after protection
    def test_flag2_protected_with_pnl_loss_is_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(
            was_protected_profit=True,
            max_unrealized_profit=3.0,
            pnl_pct=-0.5,
        )
        result = analyze_mfe_mae([trade])
        assert len(result["giveback_after_protection"]) == 1

    def test_flag2_protected_with_low_capture_is_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(
            was_protected_profit=True,
            max_unrealized_profit=4.0,
            pnl_pct=0.4,   # capture = 0.10, below 0.25 floor
        )
        result = analyze_mfe_mae([trade])
        assert len(result["giveback_after_protection"]) == 1

    def test_flag2_protected_with_good_pnl_not_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(
            was_protected_profit=True,
            max_unrealized_profit=2.0,
            pnl_pct=1.5,   # capture = 0.75, well above 0.25 floor
        )
        result = analyze_mfe_mae([trade])
        assert len(result["giveback_after_protection"]) == 0

    def test_flag2_unprotected_trade_not_in_flag2(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(was_protected_profit=False, pnl_pct=-0.5)
        result = analyze_mfe_mae([trade])
        assert len(result["giveback_after_protection"]) == 0

    # Flag 3 — never protected
    def test_flag3_no_be_no_pl_no_prot_is_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(
            was_protected_profit=False,
            break_even_armed=False,
            profit_lock_stage=0,
        )
        result = analyze_mfe_mae([trade])
        assert len(result["never_protected"]) == 1

    def test_flag3_be_armed_not_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(break_even_armed=True)
        result = analyze_mfe_mae([trade])
        assert len(result["never_protected"]) == 0

    def test_flag3_pl_stage_not_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(profit_lock_stage=1)
        result = analyze_mfe_mae([trade])
        assert len(result["never_protected"]) == 0

    def test_flag3_protected_not_flagged(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = self._make_trade(was_protected_profit=True)
        result = analyze_mfe_mae([trade])
        assert len(result["never_protected"]) == 0

    # Summary counts
    def test_summary_counts_correct(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trades = [
            # trade A: flag-1 + flag-3  (mfe=2.0, pnl=0.3 → capture=0.15 < 0.35)
            self._make_trade(max_unrealized_profit=2.0, pnl_pct=0.3),
            # trade B: flag-1 + flag-2  (protected; mfe=4.0, pnl=0.2 → capture=0.05 < 0.25)
            self._make_trade(was_protected_profit=True, max_unrealized_profit=4.0, pnl_pct=0.2),
            # trade C: clean  (protected, mfe=2.0, pnl=1.5 → capture=0.75 above both floors)
            self._make_trade(was_protected_profit=True, max_unrealized_profit=2.0, pnl_pct=1.5),
        ]
        result = analyze_mfe_mae(trades)
        s = result["summary"]
        assert s["total_closed"] == 3
        # Both trade A and trade B hit flag-1 (mfe > 1.5 AND capture < 0.35)
        assert s["high_mfe_poor_pnl_count"]        == 2
        # Only trade B hits flag-2 (protected AND capture < 0.25)
        assert s["giveback_after_protection_count"] == 1

    def test_empty_trades_returns_zero_summary(self):
        from src.tools.mfe_mae_report import analyze_mfe_mae
        result = analyze_mfe_mae([])
        s = result["summary"]
        assert s["total_closed"] == 0
        assert s["high_mfe_poor_pnl_count"] == 0
        assert s["giveback_after_protection_count"] == 0
        assert s["never_protected_count"] == 0

    def test_none_field_values_do_not_raise(self):
        """Trades with NULL MFE/MAE fields (older rows) must not raise."""
        from src.tools.mfe_mae_report import analyze_mfe_mae
        trade = {
            "trade_id": "x1",
            "asset": "ETH",
            "pnl_pct": None,
            "max_unrealized_profit": None,
            "was_protected_profit": None,
            "break_even_armed": None,
            "profit_lock_stage": None,
        }
        result = analyze_mfe_mae([trade])
        assert isinstance(result["summary"], dict)


# ─────────────────────────────────────────────────────────────────────────────
# Class 9 — generate_proposals on empty / minimal DB
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateProposals:
    def test_empty_db_returns_empty_list(self, tmp_path):
        """generate_proposals() on a brand-new empty DB must return [] without raising."""
        from src.tools.proposal_engine import generate_proposals
        db_path = str(tmp_path / "empty.db")
        results = generate_proposals(db_path=db_path)
        assert isinstance(results, list)
        assert len(results) == 0

    def test_nonexistent_db_path_returns_empty_list(self, tmp_path):
        """generate_proposals() on a path that does not exist must not raise."""
        from src.tools.proposal_engine import generate_proposals
        db_path = str(tmp_path / "subdir" / "ghost.db")
        results = generate_proposals(db_path=db_path)
        assert isinstance(results, list)

    def test_all_returned_proposals_are_drafts(self, tmp_path):
        """GUARDRAIL: every proposal from generate_proposals must be draft."""
        from src.tools.proposal_engine import generate_proposals
        db_path = str(tmp_path / "test.db")
        results = generate_proposals(db_path=db_path)
        for p in results:
            assert p.approval_status == "draft", (
                f"Proposal {p.proposal_id} has non-draft status: {p.approval_status}"
            )

    def test_generate_does_not_write_to_db(self, tmp_path):
        """generate_proposals() must not insert rows into optimization_proposals."""
        import sqlite3 as _sqlite3
        from src.tools.proposal_engine import generate_proposals
        db_path = str(tmp_path / "readonly.db")

        # Pre-create the schema so the table exists
        conn = _sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS optimization_proposals "
            "(proposal_id TEXT PRIMARY KEY, proposal_type TEXT)"
        )
        conn.commit()
        conn.close()

        generate_proposals(db_path=db_path)

        conn = _sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM optimization_proposals").fetchone()[0]
        conn.close()
        assert count == 0, "generate_proposals() must not write rows to the DB"

    def test_return_type_is_list_of_proposal_records(self, tmp_path):
        from src.tools.proposal_engine import generate_proposals, ProposalRecord
        results = generate_proposals(db_path=str(tmp_path / "typed.db"))
        assert all(isinstance(p, ProposalRecord) for p in results)

    def test_results_sorted_deterministically(self, tmp_path):
        """Two calls with the same data must return proposals in the same order."""
        from src.tools.proposal_engine import generate_proposals
        db_path = str(tmp_path / "sort.db")
        r1 = generate_proposals(db_path=db_path)
        r2 = generate_proposals(db_path=db_path)
        types1 = [p.proposal_type for p in r1]
        types2 = [p.proposal_type for p in r2]
        assert types1 == types2


# ─────────────────────────────────────────────────────────────────────────────
# Class 10 — Proposal reporter serialisers
# ─────────────────────────────────────────────────────────────────────────────

class TestProposalReporter:
    def _make_proposal_dict(self, **kwargs) -> dict:
        defaults = {
            "proposal_id":    str(uuid.uuid4()),
            "proposal_type":  "threshold_change",
            "strategy_mode":  "SCALP",
            "asset":          None,
            "current_value":  json.dumps(70),
            "proposed_value": json.dumps(75),
            "reason_summary": "score band delta > 8pp",
            "evidence_summary": "n=30 70-74: 40% vs 75-79: 50%",
            "approval_status": "draft",
            "created_at":     "2025-01-01T00:00:00+00:00",
        }
        defaults.update(kwargs)
        return defaults

    def test_proposals_to_json_returns_valid_json(self):
        from src.tools.proposal_reporter import proposals_to_json
        proposals = [self._make_proposal_dict() for _ in range(3)]
        result = proposals_to_json(proposals)
        decoded = json.loads(result)
        assert isinstance(decoded, list)
        assert len(decoded) == 3

    def test_proposals_to_json_empty_list(self):
        from src.tools.proposal_reporter import proposals_to_json
        result = proposals_to_json([])
        assert json.loads(result) == []

    def test_proposals_to_markdown_contains_heading(self):
        from src.tools.proposal_reporter import proposals_to_markdown_summary
        proposals = [self._make_proposal_dict()]
        md = proposals_to_markdown_summary(proposals)
        assert "# Optimization Proposals" in md

    def test_proposals_to_markdown_empty(self):
        from src.tools.proposal_reporter import proposals_to_markdown_summary
        md = proposals_to_markdown_summary([])
        assert "No proposals" in md

    def test_proposals_to_markdown_contains_status_table(self):
        from src.tools.proposal_reporter import proposals_to_markdown_summary
        proposals = [self._make_proposal_dict(approval_status="draft")]
        md = proposals_to_markdown_summary(proposals)
        assert "draft" in md
        assert "Status Summary" in md

    def test_proposals_to_markdown_contains_proposal_type_section(self):
        from src.tools.proposal_reporter import proposals_to_markdown_summary
        proposals = [self._make_proposal_dict(proposal_type="exit_policy_tightening")]
        md = proposals_to_markdown_summary(proposals)
        assert "Exit Policy Tightening" in md

    def test_print_proposals_table_handles_empty_list(self, capsys):
        """print_proposals_table([]) must not raise and should emit something."""
        from src.tools.proposal_reporter import print_proposals_table
        # Should not raise even with empty list
        print_proposals_table([])

    def test_proposals_to_json_accepts_proposal_record_objects(self):
        from src.tools.proposal_engine import ProposalRecord
        from src.tools.proposal_reporter import proposals_to_json
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="obj test")
        result = proposals_to_json([p])
        decoded = json.loads(result)
        assert len(decoded) == 1
        assert decoded[0]["proposal_type"] == "threshold_change"
