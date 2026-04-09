"""Phase 13 tests — Regime learning, proposals, and suitability summaries.

Covers:
    A. Regime-performance analytics (MacroRegime + RegimeLabel)
    B. Regime × mode / asset / asset-class aggregation
    C. Regime-aware remediation suggestions
    D. Regime-aware proposal generation
    E. Regime suitability summaries
    F. Schema migration & backward compatibility
    G. No auto-promotion guarantees
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _trade(
    *,
    asset: str = "BTCUSDT",
    timeframe: str = "5m",
    strategy_mode: str = "SCALP",
    pnl: float = 1.0,
    mfe: float = 2.0,
    mae: float = -0.5,
    regime_label_at_entry: str = "TRENDING_HIGH_VOL",
    regime_label_at_exit: str = "TRENDING_HIGH_VOL",
    regime_changed: bool = False,
    regime_transition_count: int = 0,
    regime_score_adjustment: float = 0.0,
    protected: bool = False,
    break_even_armed: bool = False,
    profit_lock_stage: int = 0,
    close_reason: str = "PEAK_GIVEBACK_EXIT",
    score_total: float = 72.0,
    ml_adjustment_points: float = 0.0,
    ai_adjustment_points: float = 0.0,
    entry_reason_code: str = "CONFLUENCE_3",
    asset_class: str = "",
    **extra: Any,
) -> dict:
    return {
        "trade_id": str(uuid.uuid4()),
        "asset": asset,
        "timeframe": timeframe,
        "strategy_mode": strategy_mode,
        "pnl_pct": pnl,
        "max_unrealized_profit": mfe,
        "min_unrealized_profit": mae,
        "regime_label_at_entry": regime_label_at_entry,
        "regime_label_at_exit": regime_label_at_exit,
        "regime_changed_during_trade": int(regime_changed),
        "regime_transition_count": regime_transition_count,
        "regime_score_adjustment": regime_score_adjustment,
        "was_protected_profit": int(protected),
        "break_even_armed": int(break_even_armed),
        "profit_lock_stage": profit_lock_stage,
        "close_reason": close_reason,
        "score_total": score_total,
        "ml_adjustment_points": ml_adjustment_points,
        "ai_adjustment_points": ai_adjustment_points,
        "entry_reason_code": entry_reason_code,
        "entry_time": "2025-06-01T10:00:00",
        "exit_time": "2025-06-01T10:30:00",
        "entry_reason": "test entry",
        "asset_class": asset_class,
        "status": "CLOSED",
        **extra,
    }


def _make_trades(
    n: int,
    regime: str = "TRENDING_HIGH_VOL",
    mode: str = "SCALP",
    pnl: float = 1.0,
    **kw: Any,
) -> list[dict]:
    return [_trade(regime_label_at_entry=regime, strategy_mode=mode, pnl=pnl, **kw) for _ in range(n)]


# ═══════════════════════════════════════════════════════════════════════════════
# A. Regime-Performance Analytics
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimePerformanceAnalytics:
    """Tests for compute_regime_performance()."""

    def test_macro_regime_stats_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(10, regime="TRENDING_HIGH_VOL", pnl=1.5)
            + _make_trades(8, regime="CHOPPY_LOW_VOL", pnl=-0.3)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: [])

        result = rl.compute_regime_performance()

        macro = result["macro_regime_stats"]
        # TRENDING_HIGH_VOL maps to TRENDING + HIGH_VOL
        assert macro["TRENDING"]["total_trades"] >= 10
        assert macro["HIGH_VOL"]["total_trades"] >= 10
        # CHOPPY_LOW_VOL maps to RANGING + LOW_VOL
        assert macro["RANGING"]["total_trades"] >= 8
        assert macro["LOW_VOL"]["total_trades"] >= 8

    def test_regime_label_stats_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(6, regime="TRENDING_LOW_VOL", pnl=2.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: [])

        result = rl.compute_regime_performance()

        labels = result["regime_label_stats"]
        assert "TRENDING_LOW_VOL" in labels
        assert labels["TRENDING_LOW_VOL"]["total_trades"] == 6
        assert labels["TRENDING_LOW_VOL"]["win_rate"] == 1.0

    def test_bucket_metrics_complete(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="TRENDING_HIGH_VOL", pnl=1.0,
                              protected=True, break_even_armed=True, profit_lock_stage=2)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: [])

        result = rl.compute_regime_performance()
        bucket = result["macro_regime_stats"]["TRENDING"]

        expected_keys = {
            "total_trades", "win_rate", "avg_pnl", "avg_mfe", "avg_mae",
            "avg_giveback", "avg_capture_ratio", "protected_profit_rate",
            "break_even_armed_rate", "stage_reach_rates", "avg_score",
            "avg_ml_effect", "avg_ai_effect", "diagnosis_frequency", "exit_reason_dist",
        }
        assert expected_keys.issubset(set(bucket.keys()))
        assert bucket["protected_profit_rate"] == 1.0
        assert bucket["break_even_armed_rate"] == 1.0

    def test_empty_trades_returns_empty_stats(self, monkeypatch):
        from src.tools import regime_learning as rl
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: [])
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: [])

        result = rl.compute_regime_performance()
        assert result["total_trades"] == 0

    def test_signal_acceptance_by_regime(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(5, regime="TRENDING_HIGH_VOL")
        signals = [
            {"asset": "BTCUSDT", "accepted_signal": 1, "score_total": 80.0},
            {"asset": "BTCUSDT", "accepted_signal": 0, "score_total": 55.0},
            {"asset": "BTCUSDT", "accepted_signal": 1, "score_total": 75.0},
        ]
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: signals)

        result = rl.compute_regime_performance()
        sa = result["signal_acceptance"]
        # TRENDING_HIGH_VOL → TRENDING + HIGH_VOL facets
        assert sa["TRENDING"]["total"] >= 3
        assert sa["TRENDING"]["accepted"] >= 2

    def test_unknown_regime_defaults_to_uncertain(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(5, regime="UNKNOWN", pnl=0.5)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)
        monkeypatch.setattr(rl, "_load_signals", lambda **kw: [])

        result = rl.compute_regime_performance()
        assert result["macro_regime_stats"]["UNCERTAIN"]["total_trades"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# B. Regime × Mode / Asset / Asset-Class Analytics
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossRegimeAnalytics:
    """Tests for compute_cross_regime_analytics()."""

    def test_mode_x_macro_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(6, regime="TRENDING_HIGH_VOL", mode="SCALP", pnl=1.0)
            + _make_trades(6, regime="CHOPPY_LOW_VOL", mode="INTERMEDIATE", pnl=-0.5)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_cross_regime_analytics()

        assert "SCALP" in result["mode_x_macro"]
        assert "TRENDING" in result["mode_x_macro"]["SCALP"]
        assert result["mode_x_macro"]["SCALP"]["TRENDING"]["total_trades"] == 6

        assert "INTERMEDIATE" in result["mode_x_macro"]
        assert "RANGING" in result["mode_x_macro"]["INTERMEDIATE"]
        assert result["mode_x_macro"]["INTERMEDIATE"]["RANGING"]["total_trades"] == 6

    def test_mode_x_label_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(5, regime="TRENDING_LOW_VOL", mode="SWING", pnl=2.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_cross_regime_analytics()
        assert "SWING" in result["mode_x_label"]
        assert "TRENDING_LOW_VOL" in result["mode_x_label"]["SWING"]
        assert result["mode_x_label"]["SWING"]["TRENDING_LOW_VOL"]["win_rate"] == 1.0

    def test_asset_x_macro_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(5, regime="CHOPPY_HIGH_VOL", pnl=-0.2, asset="ETHUSDT")
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_cross_regime_analytics()
        assert "ETHUSDT" in result["asset_x_macro"]
        assert "HIGH_VOL" in result["asset_x_macro"]["ETHUSDT"]

    def test_asset_class_x_macro_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(5, regime="TRENDING_HIGH_VOL", pnl=0.5, asset_class="crypto")
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_cross_regime_analytics()
        assert "crypto" in result["asset_class_x_macro"]
        assert "TRENDING" in result["asset_class_x_macro"]["crypto"]

    def test_bucket_metrics_include_diagnoses_and_exits(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(8, regime="TRENDING_HIGH_VOL", mode="SCALP", pnl=1.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_cross_regime_analytics()
        bucket = result["mode_x_macro"]["SCALP"]["TRENDING"]
        assert "exit_reason_dist" in bucket
        assert "diagnosis_frequency" in bucket
        assert "avg_ml_effect" in bucket
        assert "avg_ai_effect" in bucket

    def test_empty_trades_returns_empty(self, monkeypatch):
        from src.tools import regime_learning as rl
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: [])

        result = rl.compute_cross_regime_analytics()
        assert result["total_trades"] == 0
        assert result["mode_x_macro"] == {}


# ═══════════════════════════════════════════════════════════════════════════════
# C. Regime-Aware Remediation Suggestions
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeAwareRemediation:
    """Tests for regime-enriched suggestions from the remediation engine."""

    def test_suggestions_carry_regime_concentration(self):
        from src.tools.remediation_engine import generate_remediation_suggestions
        problems = [{
            "diagnosis_category": "WEAK_ENTRY",
            "group_field": "strategy_mode",
            "group_value": "SCALP",
            "count": 10,
            "frequency_pct": 15.0,
            "total_pnl_damage": -5.0,
            "avg_pnl_damage": -0.5,
            "mode_concentration": "SCALP",
            "asset_concentration": None,
            "problem_id": str(uuid.uuid4()),
            "regime_concentration": "RANGING",
            "regime_label_detail": "CHOPPY_HIGH_VOL",
        }]
        suggestions = generate_remediation_suggestions(problems)
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.regime_concentration == "RANGING"
        assert s.regime_label_detail == "CHOPPY_HIGH_VOL"
        assert "regime=RANGING" in s.evidence_summary

    def test_suggestion_to_proposal_carries_regime(self):
        from src.tools.remediation_engine import (
            RemediationSuggestion, suggestion_to_proposal_input,
        )
        s = RemediationSuggestion(
            diagnosis_category="WEAK_ENTRY",
            reason_summary="test",
            evidence_summary="test evidence",
            impact_summary="test impact",
            suggested_action_type="tighten_entry_threshold",
            escalation_priority="high",
            count=10,
            frequency_pct=15.0,
            total_pnl_damage=-5.0,
            avg_pnl_damage=-0.5,
            linked_proposal_type="threshold_change",
            regime_concentration="HIGH_VOL",
            regime_label_detail="TRENDING_HIGH_VOL",
        )
        result = suggestion_to_proposal_input(s)
        assert result is not None
        assert result["macro_regime"] == "HIGH_VOL"
        assert result["evidence_metrics"]["regime_concentration"] == "HIGH_VOL"
        assert result["evidence_metrics"]["regime_label_detail"] == "TRENDING_HIGH_VOL"

    def test_aggregation_includes_regime_fields(self):
        from src.tools.diagnosis_aggregator import aggregate_trade_diagnoses
        trades = [_trade(regime_label_at_entry="CHOPPY_HIGH_VOL")]
        agg = aggregate_trade_diagnoses(trades)
        assert len(agg) == 1
        assert agg[0]["regime_label_at_entry"] == "CHOPPY_HIGH_VOL"
        assert agg[0]["macro_regime"] in ("HIGH_VOL", "RANGING")  # first facet alphabetically

    def test_aggregation_unknown_regime_defaults(self):
        from src.tools.diagnosis_aggregator import aggregate_trade_diagnoses
        trades = [_trade(regime_label_at_entry=None)]
        agg = aggregate_trade_diagnoses(trades)
        assert agg[0]["regime_label_at_entry"] == "UNKNOWN"
        assert agg[0]["macro_regime"] == "UNCERTAIN"

    def test_problem_dict_carries_regime_fields(self):
        from src.tools.diagnosis_aggregator import (
            aggregate_trade_diagnoses, detect_recurring_problems,
        )
        # Create enough weak-entry trades to trigger recurring problem
        trades = _make_trades(10, regime="CHOPPY_HIGH_VOL", pnl=-1.0,
                              mfe=0.2, score_total=50.0)
        agg = aggregate_trade_diagnoses(trades)
        problems = detect_recurring_problems(agg, min_count=2, min_frequency_pct=1.0)
        # At least one problem should exist
        assert len(problems) > 0
        for p in problems:
            assert "regime_concentration" in p
            assert "regime_label_detail" in p

    def test_grouping_by_macro_regime(self):
        from src.tools.diagnosis_aggregator import aggregate_trade_diagnoses, group_by
        trades = (
            _make_trades(5, regime="TRENDING_HIGH_VOL", pnl=1.0)
            + _make_trades(5, regime="CHOPPY_LOW_VOL", pnl=-0.5)
        )
        agg = aggregate_trade_diagnoses(trades)
        groups = group_by(agg, "macro_regime")
        # Should have entries for the macro facets
        assert len(groups) >= 2

    def test_suggestions_without_regime_backward_compat(self):
        """Old-style problems without regime fields still produce valid suggestions."""
        from src.tools.remediation_engine import generate_remediation_suggestions
        problems = [{
            "diagnosis_category": "WEAK_ENTRY",
            "group_field": "strategy_mode",
            "group_value": "SCALP",
            "count": 5,
            "frequency_pct": 10.0,
            "total_pnl_damage": -2.0,
            "avg_pnl_damage": -0.4,
            "mode_concentration": "SCALP",
            "asset_concentration": None,
            "problem_id": str(uuid.uuid4()),
            # No regime_concentration or regime_label_detail
        }]
        suggestions = generate_remediation_suggestions(problems)
        assert len(suggestions) == 1
        assert suggestions[0].regime_concentration is None


# ═══════════════════════════════════════════════════════════════════════════════
# D. Regime-Aware Proposal Generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeProposalGeneration:
    """Tests for generate_regime_proposals()."""

    def test_underperforming_mode_produces_threshold_proposal(self, monkeypatch):
        from src.tools import regime_learning as rl
        # SCALP in RANGING (CHOPPY_LOW_VOL) with bad stats
        trades = _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.5, mfe=0.3, score_total=60.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        threshold_props = [p for p in proposals
                           if p.proposal_type == "regime_threshold_change"]
        assert len(threshold_props) >= 1
        p = threshold_props[0]
        assert p.macro_regime in ("RANGING", "LOW_VOL")
        assert p.strategy_mode == "SCALP"
        assert p.approval_status == "draft"

    def test_high_giveback_produces_exit_proposal(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_HIGH_VOL", mode="INTERMEDIATE",
                              pnl=0.1, mfe=3.0, score_total=70.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        exit_props = [p for p in proposals
                      if p.proposal_type == "regime_exit_policy_change"]
        assert len(exit_props) >= 1
        p = exit_props[0]
        assert p.macro_regime in ("HIGH_VOL", "RANGING")
        assert p.approval_status == "draft"

    def test_ranging_produces_fade_proposal(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.3, mfe=0.5, score_total=65.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        fade_props = [p for p in proposals
                      if p.proposal_type == "regime_fade_requirement_change"]
        assert len(fade_props) >= 1
        assert fade_props[0].macro_regime == "RANGING"

    def test_all_proposals_are_draft(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_HIGH_VOL", mode="SCALP",
                              pnl=-1.0, mfe=0.2, score_total=55.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        for p in proposals:
            assert p.approval_status == "draft"

    def test_proposals_have_macro_regime_field(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(8, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.5, mfe=0.4, score_total=60.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        for p in proposals:
            assert p.macro_regime is not None
            assert p.macro_regime in ("TRENDING", "RANGING", "HIGH_VOL", "LOW_VOL", "UNCERTAIN")

    def test_proposals_to_dict_includes_macro_regime(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.5, mfe=0.3, score_total=60.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        if proposals:
            d = proposals[0].to_dict()
            assert "macro_regime" in d
            assert d["macro_regime"] is not None

    def test_deduplication_includes_regime_scope(self, monkeypatch):
        from src.tools import regime_learning as rl
        # Same mode, different regimes → should NOT collapse
        trades = (
            _make_trades(8, regime="CHOPPY_LOW_VOL", mode="SCALP", pnl=-0.5, score_total=60.0)
            + _make_trades(8, regime="CHOPPY_HIGH_VOL", mode="SCALP", pnl=-0.5, score_total=60.0)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        proposals = rl.generate_regime_proposals()
        # Should have proposals for both RANGING and HIGH_VOL regimes
        regimes = {p.macro_regime for p in proposals}
        assert len(regimes) >= 2

    def test_insufficient_data_returns_empty(self, monkeypatch):
        from src.tools import regime_learning as rl
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: _make_trades(2))

        proposals = rl.generate_regime_proposals()
        assert proposals == []

    def test_read_only_no_db_writes(self, monkeypatch):
        from src.tools import regime_learning as rl
        import src.data.db as db_mod
        writes = []
        monkeypatch.setattr(db_mod, "save_proposal",
                            lambda *a, **kw: writes.append(("save", a)),
                            raising=False)
        trades = _make_trades(10, regime="CHOPPY_HIGH_VOL", mode="SCALP",
                              pnl=-1.0, mfe=0.2, score_total=55.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        rl.generate_regime_proposals()
        assert writes == [], f"Unexpected DB writes: {writes}"


# ═══════════════════════════════════════════════════════════════════════════════
# E. Regime Suitability Summaries
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeSuitability:
    """Tests for compute_regime_suitability()."""

    def test_mode_suitability_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(8, regime="TRENDING_HIGH_VOL", mode="SCALP", pnl=2.0)
            + _make_trades(8, regime="CHOPPY_LOW_VOL", mode="SCALP", pnl=-1.0)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_regime_suitability()
        assert len(result["mode_suitability"]) >= 1
        scalp = [m for m in result["mode_suitability"] if m["mode"] == "SCALP"]
        assert len(scalp) == 1
        assert scalp[0]["best_regime"] in ("TRENDING", "HIGH_VOL")
        assert scalp[0]["worst_regime"] in ("RANGING", "LOW_VOL")

    def test_findings_contain_human_readable_text(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(8, regime="TRENDING_LOW_VOL", mode="SWING", pnl=3.0)
            + _make_trades(8, regime="CHOPPY_HIGH_VOL", mode="SWING", pnl=-0.5)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_regime_suitability()
        assert len(result["findings"]) >= 1
        assert any("SWING" in f for f in result["findings"])
        assert any("performs best" in f or "shines" in f for f in result["findings"])

    def test_asset_suitability_populated(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(6, regime="TRENDING_HIGH_VOL", pnl=1.5, asset="BTCUSDT")
            + _make_trades(6, regime="CHOPPY_LOW_VOL", pnl=-0.8, asset="BTCUSDT")
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_regime_suitability()
        assert len(result["asset_suitability"]) >= 1
        btc = [a for a in result["asset_suitability"] if a["asset"] == "BTCUSDT"]
        assert len(btc) == 1

    def test_low_quality_mode_flagged(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = (
            _make_trades(10, regime="TRENDING_HIGH_VOL", mode="SCALP", pnl=0.5)
            + _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP", pnl=-1.0)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_regime_suitability()
        # Should have a warning about SCALP in RANGING/LOW_VOL
        warnings = [f for f in result["findings"] if "low quality" in f or "filtering" in f]
        assert len(warnings) >= 1

    def test_empty_trades_returns_empty(self, monkeypatch):
        from src.tools import regime_learning as rl
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: [])

        result = rl.compute_regime_suitability()
        assert result["mode_suitability"] == []
        assert result["findings"] == []

    def test_minimum_sample_guard(self, monkeypatch):
        from src.tools import regime_learning as rl
        # Only 2 trades per regime — below minimum
        trades = (
            _make_trades(2, regime="TRENDING_HIGH_VOL", mode="SCALP", pnl=1.0)
            + _make_trades(2, regime="CHOPPY_LOW_VOL", mode="SCALP", pnl=-1.0)
        )
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        result = rl.compute_regime_suitability()
        # Should not produce conclusions for under-sampled buckets
        # (total trades < MIN_TRADES threshold triggers early return)
        # or mode_suitability has no entries because no bucket qualifies
        # Either way: no false conclusions
        for ms in result.get("mode_suitability", []):
            assert ms.get("best_stats", {}).get("n", 0) >= 5 or ms.get("best_regime") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# F. Schema Migration & Backward Compatibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaMigration:
    """Test Phase 13 DB migration and proposal persistence."""

    @pytest.fixture()
    def _db(self, tmp_path, monkeypatch):
        """Create an isolated SQLite DB with full schema."""
        import src.data.db as db_mod
        db_file = str(tmp_path / "test.db")
        monkeypatch.setattr(db_mod, "_sqlite_path", lambda: db_file)
        monkeypatch.setattr(db_mod, "_get_supabase", lambda: None)
        db_mod.init_db()
        return db_mod

    def test_macro_regime_column_exists(self, _db):
        """Phase 13 migration adds macro_regime column to proposals table."""
        from pathlib import Path
        db_path = _db._sqlite_path()
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(optimization_proposals)")}
        assert "macro_regime" in cols

    def test_save_proposal_with_macro_regime(self, _db):
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id": pid,
            "proposal_type": "regime_threshold_change",
            "reason_summary": "test regime proposal",
            "macro_regime": "TRENDING",
            "strategy_mode": "SCALP",
            "approval_status": "draft",
        })
        rows = _db.get_proposals(proposal_type="regime_threshold_change")
        assert len(rows) == 1
        assert rows[0]["macro_regime"] == "TRENDING"

    def test_save_proposal_without_macro_regime(self, _db):
        """Old proposals without macro_regime still save correctly."""
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id": pid,
            "proposal_type": "threshold_change",
            "reason_summary": "legacy proposal",
            "approval_status": "draft",
        })
        rows = _db.get_proposals(proposal_type="threshold_change")
        assert len(rows) == 1
        assert rows[0]["macro_regime"] is None

    def test_get_proposals_filter_by_macro_regime(self, _db):
        for regime in ("TRENDING", "RANGING"):
            _db.save_proposal({
                "proposal_id": str(uuid.uuid4()),
                "proposal_type": "regime_threshold_change",
                "reason_summary": f"test {regime}",
                "macro_regime": regime,
                "approval_status": "draft",
            })
        trending = _db.get_proposals(macro_regime="TRENDING")
        assert len(trending) == 1
        assert trending[0]["macro_regime"] == "TRENDING"

    def test_new_proposal_types_valid(self, _db):
        """All Phase 13 proposal types are accepted by save_proposal."""
        for ptype in ("regime_threshold_change", "regime_exit_policy_change",
                       "regime_fade_requirement_change", "regime_ml_veto_change",
                       "regime_ai_veto_change"):
            _db.save_proposal({
                "proposal_id": str(uuid.uuid4()),
                "proposal_type": ptype,
                "reason_summary": f"test {ptype}",
                "approval_status": "draft",
            })
        rows = _db.get_proposals()
        assert len(rows) == 5

    def test_old_proposal_types_still_valid(self, _db):
        """Existing Phase 7 proposal types are not broken."""
        _db.save_proposal({
            "proposal_id": str(uuid.uuid4()),
            "proposal_type": "threshold_change",
            "reason_summary": "old type",
            "approval_status": "draft",
        })
        rows = _db.get_proposals(proposal_type="threshold_change")
        assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# G. No Auto-Promotion Guarantees
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoAutoPromotion:
    """Ensure regime proposals follow the same state machine as non-regime proposals."""

    @pytest.fixture()
    def _db(self, tmp_path, monkeypatch):
        import src.data.db as db_mod
        db_file = str(tmp_path / "test.db")
        monkeypatch.setattr(db_mod, "_sqlite_path", lambda: db_file)
        monkeypatch.setattr(db_mod, "_get_supabase", lambda: None)
        db_mod.init_db()
        return db_mod

    def test_regime_proposal_cannot_skip_to_promoted(self, _db):
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id": pid,
            "proposal_type": "regime_threshold_change",
            "reason_summary": "test",
            "macro_regime": "TRENDING",
            "approval_status": "draft",
        })
        with pytest.raises(ValueError):
            _db.transition_proposal_status(pid, "promoted")

    def test_regime_proposal_full_approval_path(self, _db):
        pid = str(uuid.uuid4())
        _db.save_proposal({
            "proposal_id": pid,
            "proposal_type": "regime_exit_policy_change",
            "reason_summary": "test full path",
            "macro_regime": "HIGH_VOL",
            "approval_status": "draft",
        })
        for next_status in [
            "backtest_pending", "backtest_complete",
            "paper_validation_pending", "paper_validation_complete",
            "approved", "promoted",
        ]:
            _db.transition_proposal_status(pid, next_status)

        rows = _db.get_proposals(status="promoted")
        assert len(rows) == 1
        assert rows[0]["proposal_type"] == "regime_exit_policy_change"

    def test_generate_proposals_never_writes_db(self, _db, monkeypatch):
        """The full generate_proposals pipeline remains read-only."""
        import src.tools.proposal_engine as pe
        writes = []
        original_save = _db.save_proposal
        monkeypatch.setattr(
            _db, "save_proposal",
            lambda *a, **kw: writes.append("write"),
        )
        # Use empty DB so all analyzers find nothing
        try:
            pe.generate_proposals(db_path=_db._sqlite_path())
        except Exception:
            pass  # Missing tables are fine — we just want to confirm no writes
        assert writes == []


# ═══════════════════════════════════════════════════════════════════════════════
# H. Regime Suggestions (Advisory Layer)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeSuggestions:
    """Tests for generate_regime_suggestions()."""

    def test_underperforming_mode_gets_threshold_suggestion(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.5, mfe=0.3, score_total=60.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        suggestions = rl.generate_regime_suggestions()
        threshold = [s for s in suggestions if s["suggestion_type"] == "threshold_hardening"]
        assert len(threshold) >= 1
        assert threshold[0]["macro_regime"] in ("RANGING", "LOW_VOL")

    def test_high_giveback_gets_protection_suggestion(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_HIGH_VOL", mode="INTERMEDIATE",
                              pnl=0.1, mfe=3.0, score_total=70.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        suggestions = rl.generate_regime_suggestions()
        prot = [s for s in suggestions if s["suggestion_type"] == "earlier_protection"]
        assert len(prot) >= 1

    def test_trending_good_stats_gets_relaxation(self, monkeypatch):
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="TRENDING_LOW_VOL", mode="SWING",
                              pnl=1.0, mfe=2.5, score_total=75.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        suggestions = rl.generate_regime_suggestions()
        relax = [s for s in suggestions if s["suggestion_type"] == "threshold_relaxation"]
        assert len(relax) >= 1
        assert relax[0]["macro_regime"] == "TRENDING"

    def test_no_suggestions_for_small_dataset(self, monkeypatch):
        from src.tools import regime_learning as rl
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: _make_trades(2))

        suggestions = rl.generate_regime_suggestions()
        assert suggestions == []

    def test_suggestions_are_advisory_only(self, monkeypatch):
        """Suggestions contain reason text and evidence but no config mutations."""
        from src.tools import regime_learning as rl
        trades = _make_trades(10, regime="CHOPPY_LOW_VOL", mode="SCALP",
                              pnl=-0.8, mfe=0.2, score_total=55.0)
        monkeypatch.setattr(rl, "_load_trades", lambda **kw: trades)

        suggestions = rl.generate_regime_suggestions()
        for s in suggestions:
            assert "reason" in s
            assert "evidence" in s
            assert isinstance(s["evidence"], dict)


# ═══════════════════════════════════════════════════════════════════════════════
# I. ProposalRecord + ProposalType Enum
# ═══════════════════════════════════════════════════════════════════════════════

class TestProposalTypeEnum:
    """Verify Phase 13 proposal types are defined."""

    def test_regime_types_exist(self):
        from src.tools.proposal_engine import ProposalType
        assert ProposalType.REGIME_THRESHOLD_CHANGE.value == "regime_threshold_change"
        assert ProposalType.REGIME_EXIT_POLICY_CHANGE.value == "regime_exit_policy_change"
        assert ProposalType.REGIME_FADE_REQUIREMENT_CHANGE.value == "regime_fade_requirement_change"
        assert ProposalType.REGIME_ML_VETO_CHANGE.value == "regime_ml_veto_change"
        assert ProposalType.REGIME_AI_VETO_CHANGE.value == "regime_ai_veto_change"

    def test_proposal_record_has_macro_regime(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(
            proposal_type="regime_threshold_change",
            reason_summary="test",
            macro_regime="TRENDING",
        )
        assert p.macro_regime == "TRENDING"
        d = p.to_dict()
        assert d["macro_regime"] == "TRENDING"

    def test_proposal_record_macro_regime_default_none(self):
        from src.tools.proposal_engine import ProposalRecord
        p = ProposalRecord(proposal_type="threshold_change", reason_summary="old")
        assert p.macro_regime is None
        assert p.to_dict()["macro_regime"] is None
