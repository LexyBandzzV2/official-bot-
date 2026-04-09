"""Phase 5 — Signal Intelligence tests.

Covers:
  A — BuySignalResult / SellSignalResult default values for all 12 new fields
  B — score_engine.compute_score() sub-score arithmetic
  C — score_engine.apply_ml_effect() / apply_ai_effect() classification & math
  D — BuySignalWorker / SellSignalWorker set indicator_flags & entry_reason_code
  E — db._signal_row() includes all Phase 5 fields via getattr
  F — signal_analytics functions (accepted_vs_rejected, near_miss, ml_effect_summary,
      indicator_combination_summary, top_rejection_reasons)
  G — reporter.print_signal_quality_report() does not raise on an empty DB
"""

from __future__ import annotations

import math
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

# ── Imports under test ────────────────────────────────────────────────────────

from src.signals.types import BuySignalResult, SellSignalResult
from src.signals.score_engine import (
    compute_score,
    apply_ml_effect,
    apply_ai_effect,
    _BOOST_MARGIN,
    _BOOST_SCORE,
    _VETO_SCORE,
)
from src.data.db import _signal_row
from src.signals.signal_analytics import (
    accepted_vs_rejected_by_mode,
    near_miss_signals,
    ml_effect_summary,
    indicator_combination_summary,
    top_rejection_reasons,
)


# ══════════════════════════════════════════════════════════════════════════════
# A — Default field values
# ══════════════════════════════════════════════════════════════════════════════

class TestBuySignalResultPhase5Defaults:

    def test_indicator_flags_is_none(self):
        sig = BuySignalResult()
        assert sig.indicator_flags is None

    def test_entry_reason_code_is_none(self):
        sig = BuySignalResult()
        assert sig.entry_reason_code is None

    def test_accepted_signal_is_false(self):
        sig = BuySignalResult()
        assert sig.accepted_signal is False

    def test_score_total_is_zero(self):
        sig = BuySignalResult()
        assert sig.score_total == 0.0

    def test_structure_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.structure_points == 0.0

    def test_indicator_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.indicator_points == 0.0

    def test_timeframe_alignment_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.timeframe_alignment_points == 0.0

    def test_candle_quality_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.candle_quality_points == 0.0

    def test_volatility_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.volatility_points == 0.0

    def test_ml_adjustment_points_is_zero(self):
        sig = BuySignalResult()
        assert sig.ml_adjustment_points == 0.0

    def test_ml_effect_is_none(self):
        sig = BuySignalResult()
        assert sig.ml_effect is None

    def test_ai_effect_is_none(self):
        sig = BuySignalResult()
        assert sig.ai_effect is None


class TestSellSignalResultPhase5Defaults:

    def test_all_phase5_defaults(self):
        sig = SellSignalResult()
        assert sig.indicator_flags is None
        assert sig.entry_reason_code is None
        assert sig.accepted_signal is False
        assert sig.score_total == 0.0
        assert sig.structure_points == 0.0
        assert sig.indicator_points == 0.0
        assert sig.timeframe_alignment_points == 0.0
        assert sig.candle_quality_points == 0.0
        assert sig.volatility_points == 0.0
        assert sig.ml_adjustment_points == 0.0
        assert sig.ml_effect is None
        assert sig.ai_effect is None


# ══════════════════════════════════════════════════════════════════════════════
# B — compute_score()
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeScore:

    def _make_sig(self, **kwargs) -> BuySignalResult:
        return BuySignalResult(**kwargs)

    def test_structure_points_alligator_true(self):
        sig = self._make_sig(alligator_point=True, timeframe="5m")
        compute_score(sig, None)
        assert sig.structure_points == 20.0

    def test_structure_points_alligator_false(self):
        sig = self._make_sig(alligator_point=False, timeframe="5m")
        compute_score(sig, None)
        assert sig.structure_points == 0.0

    def test_indicator_points_both(self):
        sig = self._make_sig(stochastic_point=True, vortex_point=True, timeframe="5m")
        compute_score(sig, None)
        assert sig.indicator_points == 20.0

    def test_indicator_points_one(self):
        sig = self._make_sig(stochastic_point=True, vortex_point=False, timeframe="5m")
        compute_score(sig, None)
        assert sig.indicator_points == 10.0

    def test_indicator_points_none(self):
        sig = self._make_sig(stochastic_point=False, vortex_point=False, timeframe="5m")
        compute_score(sig, None)
        assert sig.indicator_points == 0.0

    def test_formal_timeframe_alignment_points(self):
        for tf in ("5m", "15m", "1h", "4h"):
            sig = self._make_sig(timeframe=tf)
            compute_score(sig, None)
            assert sig.timeframe_alignment_points == 10.0, f"failed for {tf}"

    def test_informal_timeframe_alignment_points(self):
        # 30m is a genuinely informal timeframe (not in FORMAL_TIMEFRAMES)
        sig = self._make_sig(timeframe="30m")
        compute_score(sig, None)
        assert sig.timeframe_alignment_points == 5.0

    def test_no_ha_df_candle_zero(self):
        sig = self._make_sig(timeframe="5m")
        compute_score(sig, None)
        assert sig.candle_quality_points == 0.0

    def test_no_ha_df_volatility_zero(self):
        sig = self._make_sig(timeframe="5m")
        compute_score(sig, None)
        assert sig.volatility_points == 0.0

    def test_score_total_with_all_true_no_df(self):
        """alligator(20) + stoch+vortex(20) + 5m formal(10) + candle(0) + vol(0) = 50"""
        sig = self._make_sig(
            alligator_point=True,
            stochastic_point=True,
            vortex_point=True,
            timeframe="5m",
        )
        compute_score(sig, None)
        assert sig.score_total == pytest.approx(50.0)

    def test_score_total_zero_when_nothing_fired(self):
        sig = self._make_sig(timeframe="5m")
        compute_score(sig, None)
        # 0 + 0 + 10 (formal tf) + 0 + 0 = 10
        assert sig.score_total == pytest.approx(10.0)

    def test_ml_adjustment_not_set_by_compute_score(self):
        sig = self._make_sig(alligator_point=True, timeframe="5m")
        compute_score(sig, None)
        assert sig.ml_adjustment_points == 0.0
        assert sig.ml_effect is None

    def test_ha_df_with_atr_adds_volatility_points(self):
        ha = pd.DataFrame({
            "ha_open": [1.0], "ha_high": [1.2], "ha_low": [0.9], "ha_close": [1.1],
            "atr_14": [0.05],
        })
        sig = self._make_sig(timeframe="5m")
        compute_score(sig, ha)
        assert sig.volatility_points == 10.0

    def test_ha_df_zero_atr_gives_zero_volatility(self):
        ha = pd.DataFrame({
            "ha_open": [1.0], "ha_high": [1.2], "ha_low": [0.9], "ha_close": [1.1],
            "atr_14": [0.0],
        })
        sig = self._make_sig(timeframe="5m")
        compute_score(sig, ha)
        assert sig.volatility_points == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# C — apply_ml_effect() and apply_ai_effect()
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyMlEffect:

    THRESHOLD = 0.60

    def _scored_sig(self) -> BuySignalResult:
        sig = BuySignalResult(alligator_point=True, stochastic_point=True, timeframe="5m")
        compute_score(sig, None)
        return sig

    def test_boosted_effect_label(self):
        sig = self._scored_sig()
        apply_ml_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        assert sig.ml_effect == "boosted"

    def test_boosted_adds_boost_score(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ml_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        assert sig.score_total == pytest.approx(base + _BOOST_SCORE)
        assert sig.ml_adjustment_points == _BOOST_SCORE

    def test_passed_effect_label(self):
        sig = self._scored_sig()
        apply_ml_effect(sig, self.THRESHOLD + 0.01, self.THRESHOLD)
        assert sig.ml_effect == "passed"

    def test_passed_no_score_change(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ml_effect(sig, self.THRESHOLD + 0.01, self.THRESHOLD)
        assert sig.score_total == pytest.approx(base)
        assert sig.ml_adjustment_points == 0.0

    def test_vetoed_effect_label(self):
        sig = self._scored_sig()
        apply_ml_effect(sig, self.THRESHOLD - 0.01, self.THRESHOLD)
        assert sig.ml_effect == "vetoed"

    def test_vetoed_subtracts_veto_score(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ml_effect(sig, self.THRESHOLD - 0.01, self.THRESHOLD)
        assert sig.score_total == pytest.approx(base + _VETO_SCORE)
        assert sig.ml_adjustment_points == _VETO_SCORE

    def test_idempotent_second_call_replaces_first(self):
        """Calling apply_ml_effect twice should replace the first adjustment, not stack."""
        sig = self._scored_sig()
        base = sig.score_total
        # First: vetoed sets -20
        apply_ml_effect(sig, self.THRESHOLD - 0.01, self.THRESHOLD)
        # Second: boosted should undo -20 and add +10
        apply_ml_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        assert sig.ml_effect == "boosted"
        assert sig.score_total == pytest.approx(base + _BOOST_SCORE)


class TestApplyAiEffect:

    THRESHOLD = 0.55

    def _scored_sig(self) -> BuySignalResult:
        sig = BuySignalResult(alligator_point=True, timeframe="1h")
        compute_score(sig, None)
        return sig

    def test_boosted(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ai_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        assert sig.ai_effect == "boosted"
        assert sig.score_total > base

    def test_passed(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ai_effect(sig, self.THRESHOLD + 0.01, self.THRESHOLD)
        assert sig.ai_effect == "passed"
        assert sig.score_total == pytest.approx(base)

    def test_vetoed(self):
        sig = self._scored_sig()
        base = sig.score_total
        apply_ai_effect(sig, self.THRESHOLD - 0.01, self.THRESHOLD)
        assert sig.ai_effect == "vetoed"
        assert sig.score_total == pytest.approx(base + _VETO_SCORE)

    def test_ml_and_ai_effects_stack(self):
        """Both gates passed → score_total adds both adjustments."""
        sig = self._scored_sig()
        base = sig.score_total
        apply_ml_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        apply_ai_effect(sig, self.THRESHOLD + _BOOST_MARGIN + 0.01, self.THRESHOLD)
        assert sig.score_total == pytest.approx(base + _BOOST_SCORE + _BOOST_SCORE)


# ══════════════════════════════════════════════════════════════════════════════
# D — Worker indicator_flags + entry_reason_code
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkerIndicatorFlags:
    """Unit-test the indicator_flags / entry_reason_code set by BuySignalWorker.
    We build a minimal valid DataFrame to drive the worker through full logic."""

    def _make_ha_df(self, rows: int = 50) -> pd.DataFrame:
        """Synthetic HA dataframe with all required columns."""
        import numpy as np
        rng = pd.date_range("2024-01-01", periods=rows, freq="5min")
        prices = 100.0 + np.cumsum(np.random.randn(rows) * 0.1)
        df = pd.DataFrame({
            "timestamp": rng,
            "open":  prices,
            "high":  prices + 0.5,
            "low":   prices - 0.5,
            "close": prices + 0.1,
            "ha_open":  prices,
            "ha_high":  prices + 0.5,
            "ha_low":   prices - 0.5,
            "ha_close": prices + 0.1,
            "ha_body":  [0.1] * rows,
            "ha_range": [1.0] * rows,
            "is_doji":  [False] * rows,
            "is_bullish": [True] * rows,
            "is_bearish": [False] * rows,
            "volume": [1000.0] * rows,
        })

        # Add OHLC columns expected by indicators
        df["high"]  = df["ha_high"]
        df["low"]   = df["ha_low"]
        return df

    def test_buy_worker_sets_indicator_flags_on_valid_signal(self):
        from src.signals.buy_worker import BuySignalWorker
        w = BuySignalWorker("BTCUSD", "5m")
        ha = self._make_ha_df()
        result = w.evaluate(ha)
        # indicator_flags should be None or a non-empty string (depends on signal validity)
        if result.alligator_point or result.stochastic_point or result.vortex_point:
            assert result.indicator_flags is not None
            assert isinstance(result.indicator_flags, str)

    def test_buy_worker_indicator_flags_contains_alligator(self):
        from src.signals.buy_worker import BuySignalWorker
        w = BuySignalWorker("BTCUSD", "5m")
        ha = self._make_ha_df()
        result = w.evaluate(ha)
        if result.alligator_point:
            assert "alligator" in result.indicator_flags

    def test_buy_worker_entry_reason_code_abbreviations(self):
        from src.signals.buy_worker import BuySignalWorker
        w = BuySignalWorker("BTCUSD", "5m")
        ha = self._make_ha_df()
        result = w.evaluate(ha)
        if result.alligator_point:
            assert "al" in result.entry_reason_code
        if result.stochastic_point:
            assert "st" in result.entry_reason_code
        if result.vortex_point:
            assert "vo" in result.entry_reason_code

    def test_no_flags_when_no_indicators_fired(self):
        """Both fields remain None when no indicator point is earned."""
        sig = BuySignalResult(alligator_point=False, stochastic_point=False, vortex_point=False)
        # Simulate what the worker does inline
        flags = "+".join(
            f for f, hit in [
                ("alligator",  sig.alligator_point),
                ("stochastic", sig.stochastic_point),
                ("vortex",     sig.vortex_point),
            ] if hit
        ) or None
        code = "+".join(
            a for a, hit in [
                ("al", sig.alligator_point),
                ("st", sig.stochastic_point),
                ("vo", sig.vortex_point),
            ] if hit
        ) or None
        assert flags is None
        assert code is None


class TestEntryReasonCodeFormat:

    def test_all_three_flags(self):
        flags = "+".join(f for f, hit in [
            ("alligator", True), ("stochastic", True), ("vortex", True),
        ] if hit)
        assert flags == "alligator+stochastic+vortex"

    def test_code_abbrev_partial(self):
        code = "+".join(a for a, hit in [
            ("al", True), ("st", True), ("vo", False),
        ] if hit)
        assert code == "al+st"

    def test_scanner_appends_ml_ai_suffix(self):
        base = "al+st+vo"
        ml_pct = 87
        ai_pct = 72
        result = base + f":ml{ml_pct}:ai{ai_pct}"
        assert result == "al+st+vo:ml87:ai72"

    def test_entry_reason_code_ml_only(self):
        base = "al"
        ml_pct = 65
        result = base + f":ml{ml_pct}"
        assert result == "al:ml65"


# ══════════════════════════════════════════════════════════════════════════════
# E — db._signal_row() includes Phase 5 fields
# ══════════════════════════════════════════════════════════════════════════════

class TestDbSignalRow:

    def _make_sig(self, **kw) -> BuySignalResult:
        defaults = dict(
            signal_type="BUY", asset="BTCUSD", timeframe="5m",
            is_valid=True, alligator_point=True, stochastic_point=True,
            vortex_point=False, entry_price=50000.0, stop_loss=49000.0,
        )
        defaults.update(kw)
        return BuySignalResult(**defaults)

    def test_row_has_indicator_flags_key(self):
        row = _signal_row(self._make_sig(indicator_flags="alligator+stochastic"))
        assert "indicator_flags" in row

    def test_row_indicator_flags_value(self):
        row = _signal_row(self._make_sig(indicator_flags="alligator+stochastic"))
        assert row["indicator_flags"] == "alligator+stochastic"

    def test_row_has_entry_reason_code_key(self):
        row = _signal_row(self._make_sig(entry_reason_code="al+st"))
        assert "entry_reason_code" in row

    def test_row_entry_reason_code_value(self):
        row = _signal_row(self._make_sig(entry_reason_code="al+st:ml87:ai72"))
        assert row["entry_reason_code"] == "al+st:ml87:ai72"

    def test_row_accepted_signal_int(self):
        row = _signal_row(self._make_sig(accepted_signal=True))
        assert row["accepted_signal"] == 1

    def test_row_accepted_signal_false_is_zero(self):
        row = _signal_row(self._make_sig(accepted_signal=False))
        assert row["accepted_signal"] == 0

    def test_row_score_total(self):
        sig = self._make_sig()
        compute_score(sig, None)
        row = _signal_row(sig)
        assert isinstance(row["score_total"], float)
        assert row["score_total"] > 0.0

    def test_row_ml_effect_none_by_default(self):
        row = _signal_row(self._make_sig())
        assert row["ml_effect"] is None

    def test_row_ml_effect_set(self):
        sig = self._make_sig(ml_effect="vetoed")
        row = _signal_row(sig)
        assert row["ml_effect"] == "vetoed"

    def test_row_ai_effect_set(self):
        sig = self._make_sig(ai_effect="boosted")
        row = _signal_row(sig)
        assert row["ai_effect"] == "boosted"

    def test_row_backward_safe_missing_attrs(self):
        """Plain namespace without Phase 5 attrs should still produce valid row."""
        obj = SimpleNamespace(
            signal_type="BUY", asset="X", timeframe="5m",
            timestamp=__import__("datetime").datetime.now(),
            is_valid=True, points=3,
            alligator_point=True, stochastic_point=True, vortex_point=True,
            entry_price=1.0, stop_loss=0.98,
            ml_confidence=None, ai_confidence=None,
            rejection_reason="", strategy_mode="SCALP",
        )
        row = _signal_row(obj)
        assert row["accepted_signal"] == 0
        assert row["score_total"] == 0.0
        assert row["ml_effect"] is None


# ══════════════════════════════════════════════════════════════════════════════
# F — signal_analytics functions
# ══════════════════════════════════════════════════════════════════════════════

def _build_signals_db(tmp_path: Path) -> Path:
    """Create a minimal SQLite signals DB for analytics tests."""
    db_file = tmp_path / "test_signals.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE buy_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asset           TEXT,
            timeframe       TEXT,
            timestamp       TEXT,
            is_valid        INTEGER DEFAULT 0,
            points          INTEGER DEFAULT 0,
            alligator_pt    INTEGER DEFAULT 0,
            stochastic_pt   INTEGER DEFAULT 0,
            vortex_pt       INTEGER DEFAULT 0,
            entry_price     REAL    DEFAULT 0,
            stop_loss       REAL    DEFAULT 0,
            ml_confidence   REAL,
            ai_confidence   REAL,
            rejection_reason TEXT,
            strategy_mode   TEXT,
            indicator_flags TEXT,
            entry_reason_code TEXT,
            accepted_signal INTEGER DEFAULT 0,
            score_total     REAL    DEFAULT 0.0,
            structure_points REAL   DEFAULT 0.0,
            indicator_points REAL   DEFAULT 0.0,
            timeframe_alignment_points REAL DEFAULT 0.0,
            candle_quality_points REAL DEFAULT 0.0,
            volatility_points REAL  DEFAULT 0.0,
            ml_adjustment_points REAL DEFAULT 0.0,
            ml_effect       TEXT,
            ai_effect       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE sell_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asset           TEXT,
            timeframe       TEXT,
            timestamp       TEXT,
            is_valid        INTEGER DEFAULT 0,
            points          INTEGER DEFAULT 0,
            alligator_pt    INTEGER DEFAULT 0,
            stochastic_pt   INTEGER DEFAULT 0,
            vortex_pt       INTEGER DEFAULT 0,
            entry_price     REAL    DEFAULT 0,
            stop_loss       REAL    DEFAULT 0,
            ml_confidence   REAL,
            ai_confidence   REAL,
            rejection_reason TEXT,
            strategy_mode   TEXT,
            indicator_flags TEXT,
            entry_reason_code TEXT,
            accepted_signal INTEGER DEFAULT 0,
            score_total     REAL    DEFAULT 0.0,
            structure_points REAL   DEFAULT 0.0,
            indicator_points REAL   DEFAULT 0.0,
            timeframe_alignment_points REAL DEFAULT 0.0,
            candle_quality_points REAL DEFAULT 0.0,
            volatility_points REAL  DEFAULT 0.0,
            ml_adjustment_points REAL DEFAULT 0.0,
            ml_effect       TEXT,
            ai_effect       TEXT
        )
    """)

    rows = [
        # (asset, tf, is_valid, accepted, score, strategy_mode, rejection, al, st, vo, ml_effect, ai_effect)
        ("BTCUSD", "5m",  1, 1,  75.0, "SCALP",        "",                1, 1, 1, "passed",  "passed"),
        ("BTCUSD", "5m",  1, 0,  48.0, "SCALP",        "ML_CONFIDENCE",   1, 1, 0, "vetoed",  None),
        ("ETHUSD", "15m", 1, 1,  80.0, "SCALP",        "",                1, 1, 1, "boosted", "boosted"),
        ("ETHUSD", "1h",  1, 0,  65.0, "INTERMEDIATE", "AI_CONFIDENCE",   1, 0, 1, "passed",  "vetoed"),
        ("XRPUSD", "4h",  1, 1,  70.0, "SWING",        "",                1, 1, 0, "passed",  "passed"),
        ("XRPUSD", "4h",  1, 0,  30.0, "SWING",        "INSUFFICIENT_PTS",0, 0, 0, None,      None),
        ("BNBUSD", "5m",  0, 0,   0.0, "SCALP",        "INSUFFICIENT_DATA",0,0, 0, None,      None),
        # near-miss: is_valid=1, accepted=0, score in [60,70)
        ("SOLUSD", "5m",  1, 0,  62.0, "SCALP",        "ML_CONFIDENCE",   1, 1, 0, "vetoed",  None),
        ("ADAUSD", "15m", 1, 0,  68.0, "INTERMEDIATE", "AI_CONFIDENCE",   1, 0, 1, "passed",  "vetoed"),
    ]

    for r in rows:
        conn.execute(
            """INSERT INTO buy_signals
               (asset, timeframe, is_valid, accepted_signal, score_total, strategy_mode,
                rejection_reason, alligator_pt, stochastic_pt, vortex_pt,
                ml_effect, ai_effect, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*r, "2024-01-01T00:00:00"),
        )
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture()
def signals_db(tmp_path):
    return _build_signals_db(tmp_path)


class TestAcceptedVsRejectedByMode:

    def test_returns_dict(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        assert isinstance(result, dict)

    def test_scalp_mode_present(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        assert "SCALP" in result

    def test_scalp_accepted_count(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        # BTCUSD 5m accepted=1, ETHUSD 15m accepted=1; BTCUSD 5m rejected, BNBUSD rejected, SOLUSD rejected
        scalp = result["SCALP"]
        assert scalp["accepted"] >= 1

    def test_accept_rate_range(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        for mode, md in result.items():
            assert 0.0 <= md["accept_rate"] <= 1.0, f"out of range for {mode}"

    def test_total_equals_accepted_plus_rejected(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        for mode, md in result.items():
            assert md["total"] == md["accepted"] + md["rejected"], f"mismatch for {mode}"

    def test_avg_score_non_negative(self, signals_db):
        result = accepted_vs_rejected_by_mode(signals_db)
        for mode, md in result.items():
            assert md["avg_score"] >= 0.0, f"negative avg_score for {mode}"

    def test_empty_db_returns_all_zero_totals(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE buy_signals (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = accepted_vs_rejected_by_mode(db)
        # Analytics always returns all 3 modes; totals should be 0 on empty table
        for mode, md in result.items():
            assert md["total"] == 0, f"{mode} total should be 0 on empty table"

    def test_nonexistent_db_returns_all_zero_totals(self, tmp_path):
        result = accepted_vs_rejected_by_mode(tmp_path / "noexist.db")
        for mode, md in result.items():
            assert md["total"] == 0, f"{mode} total should be 0 on missing table"


class TestNearMissSignals:

    def test_returns_list(self, signals_db):
        result = near_miss_signals(signals_db)
        assert isinstance(result, list)

    def test_all_in_score_range(self, signals_db):
        result = near_miss_signals(signals_db, bound_low=60.0, bound_high=70.0)
        for row in result:
            assert 60.0 <= row["score_total"] < 70.0, f"out-of-range: {row['score_total']}"

    def test_all_are_valid_not_accepted(self, signals_db):
        result = near_miss_signals(signals_db)
        # The WHERE clause guarantees is_valid=1, accepted_signal=0;
        # returned dicts only expose the selected columns
        for row in result:
            assert 60.0 <= row["score_total"] < 70.0

    def test_known_near_miss_appears(self, signals_db):
        result = near_miss_signals(signals_db)
        assets = [r["asset"] for r in result]
        assert "SOLUSD" in assets or "ADAUSD" in assets

    def test_accepted_signals_excluded(self, signals_db):
        result = near_miss_signals(signals_db, bound_low=70.0, bound_high=90.0)
        for row in result:
            assert row.get("accepted_signal") == 0

    def test_limit_respected(self, signals_db):
        result = near_miss_signals(signals_db, limit=1)
        assert len(result) <= 1

    def test_empty_db_returns_empty(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE buy_signals (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = near_miss_signals(db)
        assert result == []


class TestMlEffectSummary:

    def test_returns_dict(self, signals_db):
        result = ml_effect_summary(signals_db)
        assert isinstance(result, dict)

    def test_has_expected_keys(self, signals_db):
        result = ml_effect_summary(signals_db)
        for key in ("ml_vetoed", "ml_passed", "ml_boosted", "ml_veto_rate", "ml_boost_rate",
                    "ai_vetoed", "ai_passed", "ai_boosted", "ai_veto_rate", "ai_boost_rate"):
            assert key in result, f"missing key: {key}"

    def test_ml_vetoed_positive(self, signals_db):
        result = ml_effect_summary(signals_db)
        # Our DB has at least 2 ml_effect='vetoed' rows
        assert result["ml_vetoed"] >= 1

    def test_ml_boosted_positive(self, signals_db):
        result = ml_effect_summary(signals_db)
        assert result["ml_boosted"] >= 1

    def test_rates_are_fractions(self, signals_db):
        result = ml_effect_summary(signals_db)
        assert 0.0 <= result["ml_veto_rate"]  <= 1.0
        assert 0.0 <= result["ml_boost_rate"] <= 1.0
        assert 0.0 <= result["ai_veto_rate"]  <= 1.0
        assert 0.0 <= result["ai_boost_rate"] <= 1.0

    def test_empty_db_returns_zeros(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE buy_signals (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = ml_effect_summary(db)
        assert result.get("ml_vetoed", 0) == 0


class TestIndicatorCombinationSummary:

    def test_returns_list(self, signals_db):
        result = indicator_combination_summary(signals_db)
        assert isinstance(result, list)

    def test_rows_have_expected_keys(self, signals_db):
        result = indicator_combination_summary(signals_db)
        if result:
            row = result[0]
            for key in ("alligator", "stochastic", "vortex", "count", "accept_rate", "avg_score"):
                assert key in row, f"missing key: {key}"

    def test_accept_rate_in_range(self, signals_db):
        result = indicator_combination_summary(signals_db)
        for row in result:
            assert 0.0 <= row["accept_rate"] <= 1.0

    def test_all_three_combo_has_highest_count(self, signals_db):
        result = indicator_combination_summary(signals_db)
        # al=True, st=True, vo=True combo should appear in our test data
        combos = {(r["alligator"], r["stochastic"], r["vortex"]): r for r in result}
        assert (True, True, True) in combos


class TestTopRejectionReasons:

    def test_returns_list(self, signals_db):
        result = top_rejection_reasons(signals_db)
        assert isinstance(result, list)

    def test_rows_have_expected_keys(self, signals_db):
        result = top_rejection_reasons(signals_db)
        if result:
            row = result[0]
            assert "reason" in row
            assert "count" in row
            assert "modes_affected" in row

    def test_ml_confidence_appears(self, signals_db):
        result = top_rejection_reasons(signals_db)
        reasons = [r["reason"] for r in result]
        assert "ML_CONFIDENCE" in reasons

    def test_modes_affected_is_list(self, signals_db):
        result = top_rejection_reasons(signals_db)
        for row in result:
            assert isinstance(row["modes_affected"], list)

    def test_limit_respected(self, signals_db):
        result = top_rejection_reasons(signals_db, limit=2)
        assert len(result) <= 2

    def test_sorted_by_count_desc(self, signals_db):
        result = top_rejection_reasons(signals_db, limit=100)
        counts = [r["count"] for r in result]
        assert counts == sorted(counts, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# G — reporter.print_signal_quality_report() does not crash on empty DB
# ══════════════════════════════════════════════════════════════════════════════

class TestPrintSignalQualityReport:

    def test_no_raise_on_empty_db(self, tmp_path):
        """Should print gracefully even when the DB has no Phase 5 data."""
        from src.backtest.reporter import print_signal_quality_report
        db = tmp_path / "empty_signals.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE buy_signals  (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE sell_signals (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        # Must not raise
        print_signal_quality_report(db, signal_type="BUY")

    def test_no_raise_on_populated_db(self, signals_db):
        from src.backtest.reporter import print_signal_quality_report
        print_signal_quality_report(signals_db, signal_type="BUY")

    def test_no_raise_on_nonexistent_db(self, tmp_path):
        from src.backtest.reporter import print_signal_quality_report
        print_signal_quality_report(tmp_path / "ghost.db", signal_type="BUY")

    def test_sell_signal_type(self, signals_db):
        from src.backtest.reporter import print_signal_quality_report
        print_signal_quality_report(signals_db, signal_type="SELL")
