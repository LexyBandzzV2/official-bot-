"""Tests for src/backtest/leakage_analyzer.py — Phase 4.

Covers:
  - All three mode keys always present
  - Zero-trade mode → no division-by-zero
  - Single trade clean stats
  - Math verification (avg_mfe, avg_giveback, capture_ratio)
  - Win-rate calculation
  - Protection rates (was_protected_profit, be_armed_rate, stage_1/2/3)
  - Trail mode rates (candle_trail, atr_trail, momentum_fade alias)
  - Fallback policy rate
  - Open trades excluded
  - print_leakage_table smoke test
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pytest

from src.backtest.leakage_analyzer import (
    analyze_leakage_by_mode,
    print_leakage_table,
)


# ── Minimal trade fixture ─────────────────────────────────────────────────────

@dataclass
class _T:
    """Minimal trade object acceptable by analyze_leakage_by_mode."""
    status:                 str   = "CLOSED"
    strategy_mode:          str   = "SCALP"
    pnl_pct:                float = 0.0
    max_unrealized_profit:  float = 0.0
    was_protected_profit:   bool  = False
    break_even_armed:       bool  = False
    profit_lock_stage:      int   = 0
    trail_active_mode:      Optional[str] = None
    used_fallback_policy:   bool  = False


def _closed(mode: str = "SCALP", **kwargs) -> _T:
    return _T(status="CLOSED", strategy_mode=mode, **kwargs)


def _open(mode: str = "SCALP", **kwargs) -> _T:
    return _T(status="OPEN", strategy_mode=mode, **kwargs)


# ── Structure tests ───────────────────────────────────────────────────────────

class TestStructure:
    def test_all_three_mode_keys_present_empty(self):
        result = analyze_leakage_by_mode([])
        assert set(result.keys()) == {"SCALP", "INTERMEDIATE", "SWING"}

    def test_all_three_mode_keys_present_with_trades(self):
        trades = [_closed("SCALP"), _closed("INTERMEDIATE")]
        result = analyze_leakage_by_mode(trades)
        assert set(result.keys()) == {"SCALP", "INTERMEDIATE", "SWING"}

    def test_zero_trade_mode_no_div_by_zero(self):
        result = analyze_leakage_by_mode([])
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            stats = result[mode]
            assert stats["count"] == 0
            assert stats["win_rate"] == 0.0
            assert stats["avg_capture_ratio"] == 0.0

    def test_expected_keys_present(self):
        result = analyze_leakage_by_mode([_closed()])
        stats = result["SCALP"]
        expected_keys = {
            "count", "win_rate", "avg_mfe", "avg_realized_pnl", "avg_giveback",
            "avg_capture_ratio", "protected_profit_rate", "be_armed_rate",
            "stage_1_rate", "stage_2_rate", "stage_3_rate",
            "candle_trail_rate", "atr_trail_rate", "momentum_fade_rate",
            "fallback_policy_rate",
        }
        assert expected_keys.issubset(stats.keys())


# ── Open trades excluded ──────────────────────────────────────────────────────

class TestOpenTradesExcluded:
    def test_open_trade_not_counted(self):
        trades = [_open("SCALP", pnl_pct=5.0), _closed("SCALP", pnl_pct=2.0)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["count"] == 1

    def test_all_open_trades_gives_zero_count(self):
        trades = [_open("SCALP"), _open("SWING")]
        result = analyze_leakage_by_mode(trades)
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert result[mode]["count"] == 0

    def test_mode_isolation_open_excluded(self):
        trades = [
            _open("SCALP", pnl_pct=10.0),
            _closed("SWING", pnl_pct=3.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["count"] == 0
        assert result["SWING"]["count"] == 1


# ── Math verification ─────────────────────────────────────────────────────────

class TestMathVerification:
    def test_avg_mfe_correct(self):
        trades = [
            _closed("SCALP", max_unrealized_profit=4.0, pnl_pct=2.0),
            _closed("SCALP", max_unrealized_profit=6.0, pnl_pct=3.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["avg_mfe"] == pytest.approx(5.0)

    def test_avg_giveback_equals_mfe_minus_pnl(self):
        trades = [
            _closed("SCALP", max_unrealized_profit=5.0, pnl_pct=2.0),
            _closed("SCALP", max_unrealized_profit=3.0, pnl_pct=1.0),
        ]
        result = analyze_leakage_by_mode(trades)
        stats = result["SCALP"]
        expected_giveback = stats["avg_mfe"] - stats["avg_realized_pnl"]
        assert stats["avg_giveback"] == pytest.approx(expected_giveback)

    def test_avg_giveback_explicit_value(self):
        trades = [
            _closed("INTERMEDIATE", max_unrealized_profit=4.0, pnl_pct=2.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["INTERMEDIATE"]["avg_giveback"] == pytest.approx(2.0)

    def test_avg_capture_ratio_correct(self):
        trades = [
            _closed("SCALP", max_unrealized_profit=4.0, pnl_pct=2.0),   # 0.50
            _closed("SCALP", max_unrealized_profit=5.0, pnl_pct=4.0),   # 0.80
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["avg_capture_ratio"] == pytest.approx((0.50 + 0.80) / 2)

    def test_avg_capture_ratio_skips_zero_mfe(self):
        """Trades with mfe=0 must be excluded from capture ratio calculation."""
        trades = [
            _closed("SCALP", max_unrealized_profit=0.0, pnl_pct=0.0),
            _closed("SCALP", max_unrealized_profit=4.0, pnl_pct=2.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["avg_capture_ratio"] == pytest.approx(0.50)

    def test_avg_capture_ratio_all_zero_mfe(self):
        """All zero MFE → capture ratio = 0 without error."""
        trades = [_closed("SCALP", max_unrealized_profit=0.0, pnl_pct=0.0)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["avg_capture_ratio"] == 0.0

    def test_win_rate_correct(self):
        trades = [
            _closed("SWING", pnl_pct=2.0),    # win
            _closed("SWING", pnl_pct=-1.0),   # loss
            _closed("SWING", pnl_pct=3.0),    # win
            _closed("SWING", pnl_pct=-0.5),   # loss
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SWING"]["win_rate"] == pytest.approx(0.50)

    def test_win_rate_all_winners(self):
        trades = [_closed("SCALP", pnl_pct=1.0) for _ in range(5)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["win_rate"] == pytest.approx(1.0)

    def test_win_rate_all_losers(self):
        trades = [_closed("SCALP", pnl_pct=-1.0) for _ in range(3)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["win_rate"] == pytest.approx(0.0)

    def test_single_trade_clean_stats(self):
        trades = [_closed("INTERMEDIATE", max_unrealized_profit=3.0, pnl_pct=2.0)]
        result = analyze_leakage_by_mode(trades)
        stats = result["INTERMEDIATE"]
        assert stats["count"] == 1
        assert stats["avg_mfe"] == pytest.approx(3.0)
        assert stats["avg_realized_pnl"] == pytest.approx(2.0)
        assert stats["avg_giveback"] == pytest.approx(1.0)
        assert stats["avg_capture_ratio"] == pytest.approx(2.0 / 3.0)
        assert stats["win_rate"] == pytest.approx(1.0)


# ── Protection rates ──────────────────────────────────────────────────────────

class TestProtectionRates:
    def test_protected_profit_rate(self):
        trades = [
            _closed("SCALP", was_protected_profit=True),
            _closed("SCALP", was_protected_profit=True),
            _closed("SCALP", was_protected_profit=False),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["protected_profit_rate"] == pytest.approx(2/3)

    def test_be_armed_rate(self):
        trades = [
            _closed("SCALP", break_even_armed=True),
            _closed("SCALP", break_even_armed=False),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["be_armed_rate"] == pytest.approx(0.5)

    def test_stage_1_rate(self):
        trades = [
            _closed("SCALP", profit_lock_stage=1),
            _closed("SCALP", profit_lock_stage=2),
            _closed("SCALP", profit_lock_stage=0),
        ]
        result = analyze_leakage_by_mode(trades)
        # stage_1_rate = profit_lock_stage >= 1 → 2/3
        assert result["SCALP"]["stage_1_rate"] == pytest.approx(2/3)

    def test_stage_2_rate(self):
        trades = [
            _closed("SCALP", profit_lock_stage=2),
            _closed("SCALP", profit_lock_stage=3),
            _closed("SCALP", profit_lock_stage=1),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["stage_2_rate"] == pytest.approx(2/3)

    def test_stage_3_rate(self):
        trades = [
            _closed("SCALP", profit_lock_stage=3),
            _closed("SCALP", profit_lock_stage=2),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["stage_3_rate"] == pytest.approx(0.5)


# ── Trail mode rates ──────────────────────────────────────────────────────────

class TestTrailModeRates:
    def test_candle_trail_rate(self):
        trades = [
            _closed("SCALP", trail_active_mode="candle_trail"),
            _closed("SCALP", trail_active_mode="atr_trail"),
            _closed("SCALP", trail_active_mode="break_even"),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["candle_trail_rate"] == pytest.approx(1/3)

    def test_atr_trail_rate(self):
        trades = [
            _closed("SCALP", trail_active_mode="atr_trail"),
            _closed("SCALP", trail_active_mode="atr_trail"),
            _closed("SCALP", trail_active_mode="candle_trail"),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["atr_trail_rate"] == pytest.approx(2/3)

    def test_momentum_fade_rate_is_alias_for_candle_trail(self):
        """momentum_fade_rate must equal candle_trail_rate (they are the same event)."""
        trades = [
            _closed("SCALP", trail_active_mode="candle_trail"),
            _closed("SCALP", trail_active_mode="atr_trail"),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["momentum_fade_rate"] == result["SCALP"]["candle_trail_rate"]

    def test_no_trail_mode_set(self):
        trades = [_closed("SCALP", trail_active_mode=None)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["candle_trail_rate"] == 0.0
        assert result["SCALP"]["atr_trail_rate"] == 0.0


# ── Fallback policy rate ──────────────────────────────────────────────────────

class TestFallbackPolicyRate:
    def test_fallback_policy_rate(self):
        trades = [
            _closed("INTERMEDIATE", used_fallback_policy=True),
            _closed("INTERMEDIATE", used_fallback_policy=False),
            _closed("INTERMEDIATE", used_fallback_policy=False),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["INTERMEDIATE"]["fallback_policy_rate"] == pytest.approx(1/3)

    def test_no_fallback_trades_gives_zero(self):
        trades = [_closed("SCALP", used_fallback_policy=False)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["fallback_policy_rate"] == 0.0

    def test_all_fallback_gives_one(self):
        trades = [_closed("SCALP", used_fallback_policy=True) for _ in range(4)]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["fallback_policy_rate"] == pytest.approx(1.0)


# ── Mode isolation ────────────────────────────────────────────────────────────

class TestModeIsolation:
    def test_scalp_trades_not_in_intermediate(self):
        trades = [
            _closed("SCALP", pnl_pct=5.0),
            _closed("INTERMEDIATE", pnl_pct=2.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["count"] == 1
        assert result["INTERMEDIATE"]["count"] == 1
        assert result["SWING"]["count"] == 0

    def test_avg_mfe_not_cross_contaminated(self):
        trades = [
            _closed("SCALP", max_unrealized_profit=10.0),
            _closed("INTERMEDIATE", max_unrealized_profit=2.0),
        ]
        result = analyze_leakage_by_mode(trades)
        assert result["SCALP"]["avg_mfe"] == pytest.approx(10.0)
        assert result["INTERMEDIATE"]["avg_mfe"] == pytest.approx(2.0)

    def test_unknown_mode_not_included(self):
        trades = [_closed("UNKNOWN", pnl_pct=5.0)]
        result = analyze_leakage_by_mode(trades)
        # UNKNOWN is not a tracked mode — all three counts are 0
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert result[mode]["count"] == 0


# ── print_leakage_table smoke test ────────────────────────────────────────────

class TestPrintLeakageTable:
    def test_no_crash_with_empty_data(self, capsys):
        print_leakage_table(analyze_leakage_by_mode([]))
        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_output_contains_all_modes(self, capsys):
        trades = [_closed(m) for m in ("SCALP", "INTERMEDIATE", "SWING")]
        print_leakage_table(analyze_leakage_by_mode(trades))
        captured = capsys.readouterr()
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert mode in captured.out

    def test_output_contains_header_labels(self, capsys):
        print_leakage_table(analyze_leakage_by_mode([]))
        captured = capsys.readouterr()
        # At minimum the table must mention mode and some stats columns
        assert "Mode" in captured.out or "mode" in captured.out.lower()

    def test_no_crash_with_mixed_trades(self, capsys):
        trades = [
            _closed("SCALP",        max_unrealized_profit=4.0, pnl_pct=2.0,
                    was_protected_profit=True, trail_active_mode="atr_trail"),
            _closed("INTERMEDIATE", max_unrealized_profit=3.0, pnl_pct=2.5,
                    break_even_armed=True),
            _open("SWING", pnl_pct=10.0),  # should be excluded
        ]
        print_leakage_table(analyze_leakage_by_mode(trades))
        captured = capsys.readouterr()
        assert "SCALP" in captured.out
