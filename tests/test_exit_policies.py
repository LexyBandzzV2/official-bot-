"""Tests for Phase 3 exit policies.

Verifies policy selection, numeric correctness, and the backward-compatibility
guarantee that IntermediateExitPolicy.giveback_frac == 0.35.
"""

from __future__ import annotations

import pytest

from src.risk.exit_policies import (
    ExitPolicy,
    ScalpExitPolicy,
    ScalpMicroExitPolicy,
    IntermediateExitPolicy,
    SwingExitPolicy,
    get_exit_policy,
)


class TestPolicySelection:
    def test_1m_returns_scalp_micro(self):
        assert get_exit_policy("1m") is ScalpMicroExitPolicy

    def test_scalp_timeframes(self):
        for tf in ("3m", "5m"):
            assert get_exit_policy(tf) is ScalpExitPolicy, tf

    def test_intermediate_timeframes(self):
        for tf in ("15m", "30m", "1h"):
            assert get_exit_policy(tf) is IntermediateExitPolicy, tf

    def test_swing_timeframes(self):
        for tf in ("2h", "3h", "4h", "1d"):
            assert get_exit_policy(tf) is SwingExitPolicy, tf

    def test_unknown_timeframe_falls_back(self):
        assert get_exit_policy("") is IntermediateExitPolicy
        assert get_exit_policy("1w") is IntermediateExitPolicy
        assert get_exit_policy("2d") is IntermediateExitPolicy


class TestBackwardCompatibility:
    def test_intermediate_giveback_frac_preserved(self):
        """Phase 2 PEAK_GIVEBACK_FRACTION was 0.35 — must not regress."""
        assert IntermediateExitPolicy.giveback_frac == pytest.approx(0.35)


class TestPolicyOrdering:
    def test_scalp_tighter_giveback_than_intermediate(self):
        assert ScalpExitPolicy.giveback_frac < IntermediateExitPolicy.giveback_frac

    def test_intermediate_tighter_giveback_than_swing(self):
        assert IntermediateExitPolicy.giveback_frac < SwingExitPolicy.giveback_frac

    def test_break_even_pct_ordering(self):
        """SCALP should arm break-even earliest (lowest threshold)."""
        assert ScalpExitPolicy.break_even_pct < IntermediateExitPolicy.break_even_pct
        assert IntermediateExitPolicy.break_even_pct < SwingExitPolicy.break_even_pct

    def test_all_policies_have_three_lock_stages(self):
        for policy in (ScalpExitPolicy, IntermediateExitPolicy, SwingExitPolicy):
            assert len(policy.profit_lock_stages) == 3, policy.name

    def test_lock_stage_thresholds_increase_monotonically(self):
        for policy in (ScalpExitPolicy, IntermediateExitPolicy, SwingExitPolicy):
            thresholds = [stage[0] for stage in policy.profit_lock_stages]
            assert thresholds == sorted(thresholds), policy.name

    def test_lock_pcts_increase_monotonically(self):
        for policy in (ScalpExitPolicy, IntermediateExitPolicy, SwingExitPolicy):
            lock_pcts = [stage[1] for stage in policy.profit_lock_stages]
            assert lock_pcts == sorted(lock_pcts), policy.name


class TestExitPolicyDataclass:
    def test_frozen_cannot_mutate(self):
        """ExitPolicy is frozen — assigning a field must raise."""
        with pytest.raises((AttributeError, TypeError)):
            ScalpExitPolicy.giveback_frac = 0.99  # type: ignore[misc]

    def test_name_field_correct(self):
        assert ScalpExitPolicy.name == "SCALP"
        assert IntermediateExitPolicy.name == "INTERMEDIATE"
        assert SwingExitPolicy.name == "SWING"
