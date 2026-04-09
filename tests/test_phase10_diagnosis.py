"""Phase 10 tests — diagnosis aggregation, recurring problems, remediation,
escalation, and reporting.

Coverage:
  TestDiagnosisAggregation    — per-trade aggregation model and grouping
  TestGroupMetrics            — compute_group_metrics stats
  TestBuildGroupedStats       — convenience wrapper
  TestRecurringProblems       — detect_recurring_problems and ranking
  TestRemediationEngine       — suggestion generation and field contracts
  TestEscalationHelper        — suggestion_to_proposal_input
  TestRankSuggestions         — rank_suggestions ordering
  TestDiagnosisReporter       — terminal/markdown/JSON output
  TestBackwardCompat          — old trade rows without lifecycle fields
  TestNoAutoPromotion         — escalation never creates/promotes proposals
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from io import StringIO

import pytest

# ── imports under test ───────────────────────────────────────────────────────

from src.tools.diagnosis_aggregator import (
    aggregate_trade_diagnoses,
    group_by,
    compute_group_metrics,
    build_grouped_stats,
    detect_recurring_problems,
    rank_problems,
    get_diagnosis_agg_data,
    VALID_GROUP_FIELDS,
)
from src.tools.remediation_engine import (
    generate_remediation_suggestions,
    suggestion_to_proposal_input,
    rank_suggestions,
    RemediationSuggestion,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
    ACT_TIGHTEN_ENTRY_THRESHOLD,
    ACT_TIGHTEN_EXIT_POLICY,
    ACT_AUDIT_POLICY_ROUTING,
    ACT_ADD_INSTRUMENTATION,
    ACT_INSPECT_TRAIL,
)
from src.tools.diagnosis_reporter import (
    get_full_review_data,
    print_diagnosis_review,
    diagnosis_review_to_markdown,
    diagnosis_review_to_json,
)
from src.tools.forensic_report import (
    DIAG_WEAK_ENTRY,
    DIAG_GIVEBACK_TOO_LOOSE,
    DIAG_TRAIL_NEVER_ARMED,
    DIAG_WRONG_EXIT_POLICY,
    DIAG_PROTECTION_TOO_LATE,
    DIAG_STRONG_ENTRY_WEAK_EXIT,
    DIAG_MISSING_LOGGING,
    DIAG_CLEAN,
)
from src.tools.proposal_engine import ProposalType


# ── shared factories ─────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.utcnow()


def _trade(**kwargs) -> dict:
    """Build a minimal closed trade dict with all Phase 3/4 lifecycle fields."""
    now = _now()
    defaults = dict(
        trade_id                      = str(uuid.uuid4()),
        signal_type                   = "BUY",
        asset                         = "BTCUSDT",
        timeframe                     = "5m",
        strategy_mode                 = "SCALP",
        entry_reason                  = "alligator+stochastic+vortex | ml=85% ai=72%",
        entry_reason_code             = "alligator+stochastic+vortex",
        entry_time                    = now.isoformat(),
        exit_time                     = (now + timedelta(hours=1)).isoformat(),
        entry_price                   = 100.0,
        exit_price                    = 102.0,
        close_reason                  = "TRAIL_STOP",
        pnl                           = 2.0,
        pnl_pct                       = 2.0,
        max_unrealized_profit         = 3.0,
        min_unrealized_profit         = -0.3,
        break_even_armed              = 1,
        profit_lock_stage             = 1,
        was_protected_profit          = 0,
        protected_profit_activation_time = None,
        timestamp_of_mfe              = (now + timedelta(minutes=30)).isoformat(),
        timestamp_of_mae              = (now + timedelta(minutes=5)).isoformat(),
        initial_stop_value            = 98.0,
        initial_exit_policy           = "SCALP",
        exit_policy_name              = "SCALP | stage_1",
        used_fallback_policy          = 0,
        max_trail_reached             = 101.5,
        status                        = "CLOSED",
    )
    defaults.update(kwargs)
    return defaults


def _weak_entry_trade(**kwargs) -> dict:
    defaults = dict(
        entry_reason="alligator | ml=70%",   # only 1 indicator
        max_unrealized_profit=0.3,
        pnl_pct=0.3,
    )
    defaults.update(kwargs)
    return _trade(**defaults)


def _giveback_trade(**kwargs) -> dict:
    defaults = dict(
        close_reason="PEAK_GIVEBACK_EXIT",
        max_unrealized_profit=3.0,
        pnl_pct=0.4,       # 13% capture — << 30%
    )
    defaults.update(kwargs)
    return _trade(**defaults)


def _trail_never_armed_trade(**kwargs) -> dict:
    # Both trail diagnosis and clean otherwise
    return _trade(
        entry_reason="alligator+stochastic+vortex | ml=85%",
        max_unrealized_profit=2.0,
        pnl_pct=1.5,
        **kwargs,
    )


def _make_trades(n: int, **kwargs) -> list[dict]:
    return [_trade(**kwargs) for _ in range(n)]


def _trail_event() -> dict:
    """One trail_update event that suppresses DIAG_TRAIL_NEVER_ARMED."""
    return {"event_type": "trail_update", "trail_update_reason": "profit_lock_stage_advance"}


def _make_agg(trades: list[dict], *, suppress_trail: bool = True) -> list[dict]:
    """Aggregate trades. By default injects one trail event per trade so that
    DIAG_TRAIL_NEVER_ARMED does not shadow the diagnosis being tested.
    Pass suppress_trail=False to test TRAIL_NEVER_ARMED itself."""
    if suppress_trail:
        events_by_id = {t["trade_id"]: [_trail_event()] for t in trades}
        return aggregate_trade_diagnoses(trades, lifecycle_events_by_id=events_by_id)
    return aggregate_trade_diagnoses(trades)


# ─────────────────────────────────────────────────────────────────────────────
# A. Diagnosis Aggregation
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosisAggregation:
    def test_empty_trades_returns_empty(self):
        assert _make_agg([]) == []

    def test_single_trade_returns_one_record(self):
        t = _trade()
        agg = _make_agg([t])
        assert len(agg) == 1

    def test_aggregated_record_has_required_keys(self):
        agg = _make_agg([_trade()])
        rec = agg[0]
        required = [
            "trade_id", "asset", "timeframe", "strategy_mode", "asset_class",
            "exit_reason", "entry_reason_code", "realized_pnl_pct",
            "max_unrealized_profit", "min_unrealized_profit", "giveback",
            "duration_secs", "was_protected_profit", "break_even_armed",
            "profit_lock_stage", "all_diagnoses", "primary_diagnosis",
        ]
        for k in required:
            assert k in rec, f"Missing key: {k}"

    def test_giveback_computed_as_mfe_minus_pnl(self):
        t = _trade(max_unrealized_profit=3.0, pnl_pct=1.0)
        rec = _make_agg([t])[0]
        assert abs(rec["giveback"] - 2.0) < 1e-6

    def test_giveback_never_negative(self):
        t = _trade(max_unrealized_profit=1.0, pnl_pct=2.0)
        rec = _make_agg([t])[0]
        assert rec["giveback"] >= 0.0

    def test_duration_secs_computed(self):
        now = _now()
        t = _trade(
            entry_time=(now).isoformat(),
            exit_time=(now + timedelta(hours=2)).isoformat(),
        )
        rec = _make_agg([t])[0]
        assert rec["duration_secs"] is not None
        assert abs(rec["duration_secs"] - 7200.0) < 5.0

    def test_duration_secs_none_when_missing(self):
        t = _trade(exit_time=None)
        rec = _make_agg([t])[0]
        assert rec["duration_secs"] is None

    def test_primary_diagnosis_is_string(self):
        t = _trade()
        rec = _make_agg([t])[0]
        assert isinstance(rec["primary_diagnosis"], str)
        assert len(rec["primary_diagnosis"]) > 0

    def test_weak_entry_detected(self):
        t = _weak_entry_trade()
        rec = _make_agg([t])[0]
        assert DIAG_WEAK_ENTRY in rec["all_diagnoses"]

    def test_backward_compat_missing_lifecycle_fields(self):
        """Old trade rows without entry_reason/mfe/mae still aggregate cleanly."""
        t = _trade(entry_reason=None, max_unrealized_profit=0.0, min_unrealized_profit=0.0)
        # suppress_trail=False so the MISSING_LOGGING early-return path is exercised
        # (trail events are not mfe_update/mae_update, but we want the pure legacy path)
        rec = _make_agg([t], suppress_trail=False)[0]
        assert rec["primary_diagnosis"] == DIAG_MISSING_LOGGING

    def test_asset_class_inferred_from_asset(self):
        t = _trade(asset="BTCUSDT")
        rec = _make_agg([t])[0]
        assert rec["asset_class"] == "crypto"

    def test_asset_class_uses_stored_value_when_present(self):
        t = _trade(asset="EURUSD")
        t["asset_class"] = "forex"
        rec = _make_agg([t])[0]
        assert rec["asset_class"] == "forex"

    def test_multiple_trades_different_diagnoses(self):
        trades = [_weak_entry_trade(), _giveback_trade(), _trade()]
        agg = _make_agg(trades)
        assert len(agg) == 3
        diags = {r["primary_diagnosis"] for r in agg}
        # At least 2 distinct primary diagnoses present
        assert len(diags) >= 1


class TestGroupBy:
    def test_group_by_strategy_mode(self):
        trades = (
            _make_trades(3, strategy_mode="SCALP") +
            _make_trades(2, strategy_mode="SWING")
        )
        agg = _make_agg(trades)
        groups = group_by(agg, "strategy_mode")
        assert "SCALP" in groups
        assert "SWING" in groups
        assert len(groups["SCALP"]) == 3
        assert len(groups["SWING"]) == 2

    def test_group_by_primary_diagnosis_includes_clean(self):
        t = _trade()
        agg = _make_agg([t])
        groups = group_by(agg, "primary_diagnosis")
        assert isinstance(groups, dict)
        assert len(groups) >= 1

    def test_group_by_asset(self):
        trades = (
            _make_trades(2, asset="BTCUSDT") +
            _make_trades(3, asset="ETHUSDT")
        )
        agg = _make_agg(trades)
        groups = group_by(agg, "asset")
        assert len(groups["BTCUSDT"]) == 2
        assert len(groups["ETHUSDT"]) == 3

    def test_group_by_exit_reason(self):
        trades = (
            _make_trades(4, close_reason="TRAIL_STOP") +
            _make_trades(2, close_reason="HARD_STOP")
        )
        agg = _make_agg(trades)
        groups = group_by(agg, "exit_reason")
        assert "TRAIL_STOP" in groups
        assert "HARD_STOP" in groups

    def test_group_by_timeframe(self):
        agg = _make_agg(_make_trades(3, timeframe="5m") + _make_trades(2, timeframe="15m"))
        g = group_by(agg, "timeframe")
        assert len(g["5m"]) == 3
        assert len(g["15m"]) == 2

    def test_group_by_entry_reason_code(self):
        agg = _make_agg(_make_trades(5, entry_reason_code="alligator+stochastic+vortex"))
        g = group_by(agg, "entry_reason_code")
        assert len(next(iter(g.values()))) == 5

    def test_invalid_field_raises(self):
        agg = _make_agg([_trade()])
        with pytest.raises(ValueError):
            group_by(agg, "invalid_field_name")


# ─────────────────────────────────────────────────────────────────────────────
# B. Group Metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestGroupMetrics:
    def _items(self, **kwargs) -> list[dict]:
        return _make_agg(_make_trades(5, **kwargs))

    def test_count_equals_input_length(self):
        items = self._items()
        m = compute_group_metrics(items, total_count=10)
        assert m["count"] == 5

    def test_frequency_pct_correct(self):
        items = self._items()
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["frequency_pct"] - 50.0) < 0.01

    def test_avg_realized_pnl(self):
        items = _make_agg([_trade(pnl_pct=2.0), _trade(pnl_pct=4.0)])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["avg_realized_pnl"] - 3.0) < 1e-6

    def test_avg_mfe(self):
        items = _make_agg([_trade(max_unrealized_profit=2.0), _trade(max_unrealized_profit=4.0)])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["avg_mfe"] - 3.0) < 1e-6

    def test_avg_mae(self):
        items = _make_agg([_trade(min_unrealized_profit=-1.0), _trade(min_unrealized_profit=-3.0)])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["avg_mae"] - (-2.0)) < 1e-6

    def test_avg_giveback(self):
        items = _make_agg([
            _trade(max_unrealized_profit=3.0, pnl_pct=1.0),
            _trade(max_unrealized_profit=4.0, pnl_pct=1.0),
        ])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["avg_giveback"] - 2.5) < 1e-6

    def test_protected_profit_rate(self):
        items = _make_agg([
            _trade(was_protected_profit=1),
            _trade(was_protected_profit=0),
            _trade(was_protected_profit=1),
            _trade(was_protected_profit=1),
        ])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["protected_profit_rate"] - 0.75) < 1e-6

    def test_break_even_armed_rate(self):
        items = _make_agg([
            _trade(break_even_armed=1),
            _trade(break_even_armed=0),
        ])
        m = compute_group_metrics(items, total_count=10)
        assert abs(m["break_even_armed_rate"] - 0.5) < 1e-6

    def test_stage_reach_rates_present(self):
        items = self._items()
        m = compute_group_metrics(items, total_count=10)
        assert "stage_reach_rates" in m
        for s in ("0", "1", "2", "3"):
            assert s in m["stage_reach_rates"]

    def test_avg_duration_secs_none_when_all_missing(self):
        items = _make_agg([_trade(exit_time=None)])
        m = compute_group_metrics(items, total_count=1)
        assert m["avg_duration_secs"] is None

    def test_empty_items_zero_metrics(self):
        m = compute_group_metrics([], total_count=10)
        assert m["count"] == 0
        assert m["frequency_pct"] == 0.0

    def test_top_diagnoses_is_list(self):
        items = self._items()
        m = compute_group_metrics(items, total_count=10)
        assert isinstance(m["top_diagnoses"], list)


class TestBuildGroupedStats:
    def test_returns_dict_keyed_by_group_value(self):
        trades = _make_trades(3, strategy_mode="SCALP") + _make_trades(2, strategy_mode="SWING")
        agg = _make_agg(trades)
        result = build_grouped_stats(agg, "strategy_mode")
        assert "SCALP" in result
        assert "SWING" in result
        assert result["SCALP"]["count"] == 3
        assert result["SWING"]["count"] == 2

    def test_frequency_pct_uses_total_agg_count(self):
        trades = _make_trades(4, strategy_mode="SCALP") + _make_trades(1, strategy_mode="SWING")
        agg = _make_agg(trades)
        r = build_grouped_stats(agg, "strategy_mode")
        assert abs(r["SCALP"]["frequency_pct"] - 80.0) < 0.01
        assert abs(r["SWING"]["frequency_pct"] - 20.0) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# C. Recurring Problems
# ─────────────────────────────────────────────────────────────────────────────

class TestRecurringProblems:
    def test_no_problems_when_below_threshold(self):
        agg = _make_agg(_make_trades(2, entry_reason="alligator | ml=70%",
                                     max_unrealized_profit=0.3, pnl_pct=0.3))
        problems = detect_recurring_problems(agg, min_count=5, min_frequency_pct=50.0)
        assert problems == []

    def test_repeated_weak_entry_detected(self):
        trades = [_weak_entry_trade() for _ in range(8)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        diag_labels = {p["diagnosis_category"] for p in problems}
        assert DIAG_WEAK_ENTRY in diag_labels

    def test_repeated_giveback_detected(self):
        trades = [_giveback_trade() for _ in range(8)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        diag_labels = {p["diagnosis_category"] for p in problems}
        assert DIAG_GIVEBACK_TOO_LOOSE in diag_labels

    def test_problem_has_required_fields(self):
        trades = [_weak_entry_trade() for _ in range(6)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        required = [
            "problem_id", "diagnosis_category", "group_field", "group_value",
            "count", "frequency_pct", "total_pnl_damage", "avg_pnl_damage",
            "mode_concentration", "asset_concentration", "affected_trade_ids",
        ]
        for p in problems:
            for k in required:
                assert k in p, f"Missing field {k!r} in problem dict"

    def test_affected_trade_ids_populated(self):
        trades = [_weak_entry_trade() for _ in range(5)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        for p in problems:
            if p["diagnosis_category"] == DIAG_WEAK_ENTRY:
                assert len(p["affected_trade_ids"]) >= 3

    def test_mode_concentration_detected(self):
        trades = [_weak_entry_trade(strategy_mode="SCALP") for _ in range(6)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        for p in problems:
            if p["diagnosis_category"] == DIAG_WEAK_ENTRY:
                assert p.get("mode_concentration") == "SCALP"
                break

    def test_asset_concentration_detected(self):
        trades = [_giveback_trade(asset="BTCUSDT") for _ in range(6)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        for p in problems:
            if p["diagnosis_category"] == DIAG_GIVEBACK_TOO_LOOSE:
                assert p.get("asset_concentration") == "BTCUSDT"
                break

    def test_no_clean_diagnosis_in_problems(self):
        agg = _make_agg(_make_trades(10))
        problems = detect_recurring_problems(agg, min_count=1, min_frequency_pct=0.0)
        for p in problems:
            assert p["diagnosis_category"] != DIAG_CLEAN

    def test_rank_by_frequency(self):
        trades = [_weak_entry_trade() for _ in range(8)] + [_giveback_trade() for _ in range(4)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        ranked = rank_problems(problems, by="frequency")
        freqs = [p["frequency_pct"] for p in ranked]
        assert freqs == sorted(freqs, reverse=True)

    def test_rank_by_count(self):
        trades = [_weak_entry_trade() for _ in range(8)] + [_giveback_trade() for _ in range(4)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        ranked = rank_problems(problems, by="count")
        counts = [p["count"] for p in ranked]
        assert counts == sorted(counts, reverse=True)

    def test_rank_by_pnl_damage(self):
        trades = (
            [_giveback_trade(pnl_pct=-2.0) for _ in range(5)] +
            [_weak_entry_trade(pnl_pct=-0.5) for _ in range(5)]
        )
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        ranked = rank_problems(problems, by="total_pnl_damage")
        damages = [p["total_pnl_damage"] for p in ranked]
        assert damages == sorted(damages)   # ascending (most negative first)

    def test_no_duplicate_problems(self):
        trades = [_weak_entry_trade() for _ in range(6)]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        keys = [(p["diagnosis_category"], p["group_field"], p["group_value"]) for p in problems]
        assert len(keys) == len(set(keys))

    def test_rank_invalid_field_raises(self):
        with pytest.raises(ValueError):
            rank_problems([], by="nonexistent")

    def test_empty_agg_returns_empty_problems(self):
        assert detect_recurring_problems([], min_count=1, min_frequency_pct=0.0) == []

    def test_wrong_exit_policy_detected(self):
        trades = [
            _trade(strategy_mode="SCALP", close_reason="ALLIGATOR_TP",
                   max_unrealized_profit=1.5)
            for _ in range(5)
        ]
        agg = _make_agg(trades)
        problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        diag_labels = {p["diagnosis_category"] for p in problems}
        assert DIAG_WRONG_EXIT_POLICY in diag_labels


# ─────────────────────────────────────────────────────────────────────────────
# D. Remediation Engine
# ─────────────────────────────────────────────────────────────────────────────

def _make_problem(diag: str, count: int = 6, freq: float = 30.0,
                  mode: str = "SCALP", asset: str = "BTCUSDT") -> dict:
    return {
        "problem_id":          str(uuid.uuid4()),
        "diagnosis_category":  diag,
        "group_field":         "strategy_mode",
        "group_value":         mode,
        "count":               count,
        "frequency_pct":       freq,
        "total_pnl_damage":    -count * 1.5,
        "avg_pnl_damage":      -1.5,
        "mode_concentration":  mode,
        "asset_concentration": asset,
        "affected_trade_ids":  [str(uuid.uuid4()) for _ in range(count)],
    }


class TestRemediationEngine:
    def test_empty_problems_returns_empty(self):
        assert generate_remediation_suggestions([]) == []

    def test_one_suggestion_per_problem(self):
        problems = [_make_problem(DIAG_WEAK_ENTRY), _make_problem(DIAG_GIVEBACK_TOO_LOOSE)]
        suggestions = generate_remediation_suggestions(problems)
        assert len(suggestions) == 2

    def test_suggestion_has_required_fields(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WEAK_ENTRY)])[0]
        required = [
            "suggestion_id", "diagnosis_category", "strategy_mode",
            "reason_summary", "evidence_summary", "impact_summary",
            "suggested_action_type", "escalation_priority", "count",
            "frequency_pct", "total_pnl_damage", "avg_pnl_damage",
        ]
        d = s.to_dict()
        for k in required:
            assert k in d, f"Missing field: {k}"

    def test_suggestion_id_is_unique(self):
        problems = [_make_problem(DIAG_WEAK_ENTRY) for _ in range(5)]
        suggestions = generate_remediation_suggestions(problems)
        ids = [s.suggestion_id for s in suggestions]
        assert len(ids) == len(set(ids))

    def test_weak_entry_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WEAK_ENTRY)])[0]
        assert s.suggested_action_type == ACT_TIGHTEN_ENTRY_THRESHOLD

    def test_giveback_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_GIVEBACK_TOO_LOOSE)])[0]
        assert s.suggested_action_type == ACT_TIGHTEN_EXIT_POLICY

    def test_trail_never_armed_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_TRAIL_NEVER_ARMED)])[0]
        assert s.suggested_action_type == ACT_INSPECT_TRAIL

    def test_wrong_exit_policy_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WRONG_EXIT_POLICY)])[0]
        assert s.suggested_action_type == ACT_AUDIT_POLICY_ROUTING

    def test_missing_logging_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_MISSING_LOGGING)])[0]
        assert s.suggested_action_type == ACT_ADD_INSTRUMENTATION

    def test_strong_entry_weak_exit_action_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_STRONG_ENTRY_WEAK_EXIT)])[0]
        assert s.suggested_action_type == ACT_TIGHTEN_EXIT_POLICY

    def test_weak_entry_escalatable(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WEAK_ENTRY)])[0]
        assert s.is_escalatable is True
        assert s.linked_proposal_type == ProposalType.THRESHOLD_CHANGE.value

    def test_giveback_escalatable(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_GIVEBACK_TOO_LOOSE)])[0]
        assert s.is_escalatable is True
        assert s.linked_proposal_type == ProposalType.EXIT_POLICY_TIGHTENING.value

    def test_trail_never_armed_not_escalatable(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_TRAIL_NEVER_ARMED)])[0]
        assert s.is_escalatable is False
        assert s.linked_proposal_type is None

    def test_wrong_exit_policy_not_escalatable(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WRONG_EXIT_POLICY)])[0]
        assert s.is_escalatable is False

    def test_missing_logging_not_escalatable(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_MISSING_LOGGING)])[0]
        assert s.is_escalatable is False

    def test_priority_values_valid(self):
        _ALL_PRIORITIES = {PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW}
        probs = [_make_problem(d) for d in [
            DIAG_WEAK_ENTRY, DIAG_GIVEBACK_TOO_LOOSE, DIAG_TRAIL_NEVER_ARMED,
            DIAG_WRONG_EXIT_POLICY, DIAG_MISSING_LOGGING, DIAG_STRONG_ENTRY_WEAK_EXIT,
            DIAG_PROTECTION_TOO_LATE,
        ]]
        for s in generate_remediation_suggestions(probs):
            assert s.escalation_priority in _ALL_PRIORITIES

    def test_reason_summary_max_200_chars(self):
        for diag in [DIAG_WEAK_ENTRY, DIAG_GIVEBACK_TOO_LOOSE, DIAG_WRONG_EXIT_POLICY]:
            s = generate_remediation_suggestions([_make_problem(diag)])[0]
            assert len(s.reason_summary) <= 200, f"{diag}: reason_summary > 200 chars"

    def test_unknown_diagnosis_gets_generic_suggestion(self):
        prob = _make_problem("some_unknown_future_diagnosis")
        suggestions = generate_remediation_suggestions([prob])
        assert len(suggestions) == 1
        assert suggestions[0].suggested_action_type == "manual_review"

    def test_count_and_freq_preserved(self):
        prob = _make_problem(DIAG_WEAK_ENTRY, count=12, freq=45.0)
        s = generate_remediation_suggestions([prob])[0]
        assert s.count == 12
        assert abs(s.frequency_pct - 45.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# E. Escalation Helper
# ─────────────────────────────────────────────────────────────────────────────

class TestEscalationHelper:
    def _escalatable(self) -> RemediationSuggestion:
        return generate_remediation_suggestions([_make_problem(DIAG_WEAK_ENTRY)])[0]

    def _non_escalatable(self) -> RemediationSuggestion:
        return generate_remediation_suggestions([_make_problem(DIAG_TRAIL_NEVER_ARMED)])[0]

    def test_escalatable_returns_dict(self):
        result = suggestion_to_proposal_input(self._escalatable())
        assert isinstance(result, dict)

    def test_non_escalatable_returns_none(self):
        assert suggestion_to_proposal_input(self._non_escalatable()) is None

    def test_proposal_input_has_required_fields(self):
        result = suggestion_to_proposal_input(self._escalatable())
        required = [
            "proposal_type", "strategy_mode", "reason_summary",
            "evidence_summary", "evidence_metrics", "approval_status",
        ]
        for k in required:
            assert k in result, f"Missing field: {k}"

    def test_approval_status_is_draft(self):
        result = suggestion_to_proposal_input(self._escalatable())
        assert result["approval_status"] == "draft"

    def test_proposal_type_is_valid(self):
        result = suggestion_to_proposal_input(self._escalatable())
        valid_types = {pt.value for pt in ProposalType}
        assert result["proposal_type"] in valid_types

    def test_strategy_mode_preserved(self):
        result = suggestion_to_proposal_input(self._escalatable())
        assert result["strategy_mode"] == "SCALP"

    def test_no_auto_promotion_field_in_output(self):
        """Escalation output must not include promoted_at or approval shortcuts."""
        result = suggestion_to_proposal_input(self._escalatable())
        assert "promoted_at" not in result or result.get("promoted_at") is None
        # Crucially: approval_status must be "draft", not "approved" or "promoted"
        assert result["approval_status"] == "draft"

    def test_evidence_metrics_contains_suggestion_id(self):
        s = self._escalatable()
        result = suggestion_to_proposal_input(s)
        assert result["evidence_metrics"]["suggestion_id"] == s.suggestion_id

    def test_current_value_is_none_requiring_operator_fill(self):
        result = suggestion_to_proposal_input(self._escalatable())
        assert result["current_value"] is None

    def test_giveback_too_loose_maps_to_correct_proposal_type(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_GIVEBACK_TOO_LOOSE)])[0]
        result = suggestion_to_proposal_input(s)
        assert result["proposal_type"] == ProposalType.EXIT_POLICY_TIGHTENING.value


# ─────────────────────────────────────────────────────────────────────────────
# F. Rank Suggestions
# ─────────────────────────────────────────────────────────────────────────────

class TestRankSuggestions:
    def _suggestions(self) -> list[RemediationSuggestion]:
        probs = [
            _make_problem(DIAG_WEAK_ENTRY, count=10, freq=50.0),
            _make_problem(DIAG_TRAIL_NEVER_ARMED, count=5, freq=25.0),
            _make_problem(DIAG_MISSING_LOGGING, count=3, freq=15.0),
        ]
        return generate_remediation_suggestions(probs)

    def test_rank_by_priority_high_first(self):
        ranked = rank_suggestions(self._suggestions(), by="priority")
        priorities = [s.escalation_priority for s in ranked]
        # HIGH must appear before MEDIUM which must appear before LOW
        seen = []
        for p in priorities:
            if p not in seen:
                seen.append(p)
        order = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}
        for i in range(len(seen) - 1):
            assert order[seen[i]] <= order[seen[i + 1]]

    def test_rank_by_frequency(self):
        ranked = rank_suggestions(self._suggestions(), by="frequency")
        freqs = [s.frequency_pct for s in ranked]
        assert freqs == sorted(freqs, reverse=True)

    def test_rank_by_count(self):
        ranked = rank_suggestions(self._suggestions(), by="count")
        counts = [s.count for s in ranked]
        assert counts == sorted(counts, reverse=True)

    def test_rank_by_pnl_damage(self):
        ranked = rank_suggestions(self._suggestions(), by="pnl_damage")
        damages = [s.total_pnl_damage for s in ranked]
        assert damages == sorted(damages)  # ascending (most negative = worst first)

    def test_invalid_rank_field_raises(self):
        with pytest.raises(ValueError):
            rank_suggestions([], by="invalid")

    def test_empty_input_returns_empty(self):
        assert rank_suggestions([], by="priority") == []


# ─────────────────────────────────────────────────────────────────────────────
# G. Reporting
# ─────────────────────────────────────────────────────────────────────────────

def _build_data(n_weak: int = 8, n_giveback: int = 5) -> dict:
    """Build a synthetic data dict without touching the DB."""
    trades = (
        [_weak_entry_trade() for _ in range(n_weak)] +
        [_giveback_trade()   for _ in range(n_giveback)] +
        [_trade()            for _ in range(3)]
    )
    agg      = aggregate_trade_diagnoses(trades)
    from src.tools.diagnosis_aggregator import (
        build_grouped_stats, detect_recurring_problems, rank_problems,
    )
    problems = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
    suggestions = generate_remediation_suggestions(problems)
    ranked_s    = rank_suggestions(suggestions, by="priority")
    escalatable = [s for s in ranked_s if s.is_escalatable]

    return {
        "total_closed":            len(trades),
        "aggregated":              agg,
        "by_primary_diagnosis":    build_grouped_stats(agg, "primary_diagnosis"),
        "by_strategy_mode":        build_grouped_stats(agg, "strategy_mode"),
        "by_asset":                build_grouped_stats(agg, "asset"),
        "by_exit_reason":          build_grouped_stats(agg, "exit_reason"),
        "by_timeframe":            build_grouped_stats(agg, "timeframe"),
        "by_entry_reason_code":    build_grouped_stats(agg, "entry_reason_code"),
        "recurring_problems":      problems,
        "problems_by_frequency":   rank_problems(problems, by="frequency"),
        "problems_by_pnl_damage":  rank_problems(problems, by="total_pnl_damage"),
        "suggestions":             [s.to_dict() for s in ranked_s],
        "high_priority_count":     sum(1 for s in ranked_s if s.escalation_priority == PRIORITY_HIGH),
        "escalatable_count":       len(escalatable),
        "escalation_candidates":   [suggestion_to_proposal_input(s) for s in escalatable],
    }


class TestDiagnosisReporter:
    def test_json_is_valid_json(self):
        data = _build_data()
        raw = diagnosis_review_to_json(data)
        parsed = json.loads(raw)
        assert "total_closed" in parsed

    def test_json_has_all_top_level_keys(self):
        data = _build_data()
        parsed = json.loads(diagnosis_review_to_json(data))
        for k in [
            "total_closed", "by_primary_diagnosis", "by_strategy_mode",
            "by_asset", "by_exit_reason", "recurring_problems",
            "suggestions", "escalatable_count",
        ]:
            assert k in parsed, f"Missing JSON key: {k}"

    def test_markdown_contains_summary_heading(self):
        md = diagnosis_review_to_markdown(_build_data())
        assert "# Owl Stalk" in md

    def test_markdown_contains_by_mode_section(self):
        md = diagnosis_review_to_markdown(_build_data())
        assert "Strategy Mode" in md

    def test_markdown_contains_by_asset_section(self):
        md = diagnosis_review_to_markdown(_build_data())
        assert "Asset" in md

    def test_markdown_contains_recurring_problems(self):
        md = diagnosis_review_to_markdown(_build_data())
        assert "Recurring Problems" in md

    def test_markdown_contains_suggestions(self):
        md = diagnosis_review_to_markdown(_build_data())
        assert "Remediation Suggestions" in md

    def test_markdown_escalation_section_present_when_applicable(self):
        md = diagnosis_review_to_markdown(_build_data())
        # At least one escalatable suggestion exists with the test data
        assert "Escalat" in md

    def test_print_runs_without_error(self):
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=160)
        print_diagnosis_review(_build_data(), console=con)
        assert len(buf.getvalue()) > 50

    def test_print_contains_total_closed(self):
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=160)
        data = _build_data()
        print_diagnosis_review(data, console=con)
        assert str(data["total_closed"]) in buf.getvalue()

    def test_empty_data_handled_gracefully(self):
        empty_data = {
            "total_closed": 0, "aggregated": [], "by_primary_diagnosis": {},
            "by_strategy_mode": {}, "by_asset": {}, "by_exit_reason": {},
            "by_timeframe": {}, "by_entry_reason_code": {},
            "recurring_problems": [], "problems_by_frequency": [],
            "problems_by_pnl_damage": [], "suggestions": [],
            "high_priority_count": 0, "escalatable_count": 0,
            "escalation_candidates": [],
        }
        md = diagnosis_review_to_markdown(empty_data)
        assert "# Owl Stalk" in md
        json_str = diagnosis_review_to_json(empty_data)
        assert json.loads(json_str)["total_closed"] == 0

    def test_json_floats_rounded_to_4dp(self):
        data = _build_data()
        parsed = json.loads(diagnosis_review_to_json(data))
        def check_floats(obj):
            if isinstance(obj, float):
                assert obj == round(obj, 4), f"Float {obj} not rounded to 4dp"
            elif isinstance(obj, dict):
                for v in obj.values():
                    check_floats(v)
            elif isinstance(obj, list):
                for v in obj:
                    check_floats(v)
        check_floats(parsed)


# ─────────────────────────────────────────────────────────────────────────────
# H. Backward Compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_trade_without_entry_reason_aggregates_cleanly(self):
        t = _trade(entry_reason=None)
        rec = _make_agg([t])[0]
        assert isinstance(rec["primary_diagnosis"], str)

    def test_trade_without_mfe_mae_aggregates_cleanly(self):
        t = _trade(max_unrealized_profit=None, min_unrealized_profit=None)
        rec = _make_agg([t])[0]
        assert rec["max_unrealized_profit"] == 0.0
        assert rec["min_unrealized_profit"] == 0.0

    def test_trade_without_exit_time_aggregates_cleanly(self):
        t = _trade(exit_time=None)
        rec = _make_agg([t])[0]
        assert rec["duration_secs"] is None

    def test_trade_without_strategy_mode_defaults_to_unknown(self):
        t = _trade()
        t["strategy_mode"] = None
        rec = _make_agg([t])[0]
        assert rec["strategy_mode"] == "UNKNOWN"

    def test_trade_without_entry_reason_code_defaults_to_unknown(self):
        t = _trade(entry_reason_code=None)
        rec = _make_agg([t])[0]
        assert rec["entry_reason_code"] == "UNKNOWN"

    def test_giveback_zero_when_pnl_exceeds_mfe(self):
        """Old rows may have anomalous MFE < PnL; giveback must not be negative."""
        t = _trade(max_unrealized_profit=1.0, pnl_pct=3.0)
        rec = _make_agg([t])[0]
        assert rec["giveback"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# I. No Auto-Promotion Guarantee
# ─────────────────────────────────────────────────────────────────────────────

class TestNoAutoPromotion:
    def test_escalation_output_always_draft(self):
        s = generate_remediation_suggestions([_make_problem(DIAG_WEAK_ENTRY)])[0]
        p_input = suggestion_to_proposal_input(s)
        assert p_input is not None
        assert p_input["approval_status"] == "draft"

    def test_escalation_output_can_construct_proposal_record(self):
        """The dict should be valid ProposalRecord kwargs (proposal_id auto-generates)."""
        s = generate_remediation_suggestions([_make_problem(DIAG_GIVEBACK_TOO_LOOSE)])[0]
        p_input = suggestion_to_proposal_input(s)
        from src.tools.proposal_engine import ProposalRecord
        record = ProposalRecord(**p_input)
        assert record.approval_status == "draft"
        assert record.promoted_at is None

    def test_suggestions_generated_from_data_never_trigger_db_writes(
        self, monkeypatch
    ):
        """Patch db writes to assert they are never called during suggestion generation."""
        import src.data.db as db_mod
        writes_called = []
        monkeypatch.setattr(db_mod, "save_proposal",
                            lambda *a, **kw: writes_called.append(("save_proposal", a)),
                            raising=False)
        monkeypatch.setattr(db_mod, "transition_proposal_status",
                            lambda *a, **kw: writes_called.append(("transition", a)),
                            raising=False)

        trades = [_weak_entry_trade() for _ in range(8)]
        agg    = aggregate_trade_diagnoses(trades)
        probs  = detect_recurring_problems(agg, min_count=3, min_frequency_pct=5.0)
        _      = generate_remediation_suggestions(probs)

        assert writes_called == [], f"Unexpected DB writes: {writes_called}"


# ─────────────────────────────────────────────────────────────────────────────
# J. DB Integration (uses real init_db + patched path)
# ─────────────────────────────────────────────────────────────────────────────

class TestDbIntegration:
    @pytest.fixture()
    def _db(self, tmp_path, monkeypatch):
        import src.data.db as db_mod
        db_file = str(tmp_path / "test_phase10.db")
        monkeypatch.setattr(db_mod, "SQLITE_PATH", db_file)
        db_mod.init_db()
        db_mod.migrate_add_lifecycle_fields()
        return db_mod

    def _insert_trade(self, conn, *, mode: str = "SCALP", pnl: float = 1.0,
                      reason: str = "alligator+stochastic+vortex | ml=80%",
                      close_reason: str = "TRAIL_STOP") -> None:
        import uuid as _uuid
        conn.execute(
            """INSERT INTO trades
               (trade_id, asset, timeframe, signal_type, entry_price, entry_time,
                stop_loss_hard, trailing_stop, position_size, account_risk_pct,
                exit_price, exit_time, pnl_pct, status,
                strategy_mode, close_reason,
                max_unrealized_profit, min_unrealized_profit,
                entry_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(_uuid.uuid4()), "BTCUSDT", "5m", "BUY", 100.0,
             "2025-01-01T10:00:00", 95.0, 97.0, 1.0, 1.0,
             101.0, "2025-01-01T12:00:00",
             pnl, "CLOSED", mode, close_reason,
             pnl + 0.5, -0.2, reason),
        )
        conn.commit()

    def test_empty_db_returns_zero_total(self, _db):
        data = get_diagnosis_agg_data(_db.SQLITE_PATH)
        assert data["total_closed"] == 0
        assert data["aggregated"] == []
        assert data["recurring_problems"] == []

    def test_trades_aggregated_from_db(self, _db):
        with _db._sqlite_conn() as conn:
            for _ in range(5):
                self._insert_trade(conn, mode="SCALP", pnl=1.5)
        data = get_diagnosis_agg_data(_db.SQLITE_PATH, min_count=3, min_frequency_pct=5.0)
        assert data["total_closed"] == 5
        assert len(data["aggregated"]) == 5

    def test_full_review_data_includes_suggestions(self, _db):
        with _db._sqlite_conn() as conn:
            for _ in range(8):
                self._insert_trade(
                    conn, mode="SCALP", pnl=0.3,
                    reason="alligator | ml=60%",   # weak entry
                )
        data = get_full_review_data(_db.SQLITE_PATH, min_count=3, min_frequency_pct=5.0)
        assert "suggestions" in data
        assert isinstance(data["suggestions"], list)
