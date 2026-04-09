"""Phase 6 candle-strength fade tests.

Covers:
  - FadeAnalysis dataclass and evidence_summary()
  - evaluate_fade() for BUY and SELL directions
  - confirmation_bars requirement (SCALP: 2 bars, avoid single-doji trigger)
  - ExitPolicy Phase 6 fade fields and ScalpExitPolicy defaults
  - TradeRecord Phase 6 fade observability fields
  - DB migration: migrate_add_fade_observability_fields()
  - update_trade_lifecycle whitelist for the 3 new fields
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import fields as dc_fields
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def _candle(open_, high, low, close):
    return (float(open_), float(high), float(low), float(close))


def _strong_buy_candle(body: float = 0.80) -> tuple:
    """Return a BUY candle with the given body_to_range_ratio (range = 1.0)."""
    # body = abs(close - open) / (high - low)  → close - open = body (range=1)
    # Set open=0.10, high=1.0, low=0.0, close=0.10+body
    return _candle(0.10, 1.0, 0.0, 0.10 + body)


def _weak_candle(body: float = 0.20) -> tuple:
    """Return a candle with a small body (nearly a doji)."""
    return _candle(0.50, 1.0, 0.0, 0.50 + body)


def _shrinking_buy_sequence(n: int = 3) -> list[tuple]:
    """Return window+1 candles with strictly shrinking bodies for BUY.

    Bodies (for n=3): 0.80, 0.55, 0.35, 0.15  (4 candles total)
    """
    bodies = [0.80, 0.55, 0.35, 0.15]  # length = n+1 for n=3
    assert len(bodies) >= n + 1
    return [_strong_buy_candle(b) for b in bodies[: n + 1]]


# ---------------------------------------------------------------------------
# Tests: FadeAnalysis and evidence_summary
# ---------------------------------------------------------------------------

class TestFadeAnalysisEvidenceSummary(unittest.TestCase):
    def test_format(self):
        from src.risk.candle_quality import evaluate_fade

        candles = _shrinking_buy_sequence(3)
        fa = evaluate_fade(candles, "BUY", window=3, confirmation_bars=1)

        summary = fa.evidence_summary()
        self.assertIn("fade=", summary)
        self.assertIn("body=[", summary)
        self.assertIn("wick=[", summary)
        self.assertIn("strong=", summary)
        self.assertIn("confirm=", summary)

    def test_false_when_not_fade(self):
        from src.risk.candle_quality import evaluate_fade

        # All strong candles — no shrink, no weak
        candles = [_strong_buy_candle(0.80)] * 4
        fa = evaluate_fade(candles, "BUY", window=3, confirmation_bars=1)
        self.assertFalse(fa.fade_detected)
        self.assertIn("fade=False", fa.evidence_summary())

    def test_confirm_fraction_in_summary(self):
        from src.risk.candle_quality import evaluate_fade

        # 2 weak candles in a 3-bar window — expected "confirm=2/3"
        candles = _shrinking_buy_sequence(3)
        fa = evaluate_fade(
            candles, "BUY", window=3,
            weak_body_threshold=0.50,   # body 0.55 is > 0.50, 0.35 and 0.15 are < 0.50
            confirmation_bars=1,
        )
        summary = fa.evidence_summary()
        # confirm=N/3 — N depends on window contents
        self.assertRegex(summary, r"confirm=\d/3")


# ---------------------------------------------------------------------------
# Tests: evaluate_fade — boundary / disabled cases
# ---------------------------------------------------------------------------

class TestEvaluateFadeDisabled(unittest.TestCase):
    def test_window_zero_returns_empty(self):
        from src.risk.candle_quality import evaluate_fade

        candles = _shrinking_buy_sequence(3)
        fa = evaluate_fade(candles, "BUY", window=0)
        self.assertFalse(fa.fade_detected)
        self.assertEqual(fa.body_ratios, [])
        self.assertEqual(fa.consecutive_strong_candles, 0)

    def test_insufficient_candles_returns_empty(self):
        from src.risk.candle_quality import evaluate_fade

        # window=3 needs 4 candles; supply only 3
        fa = evaluate_fade([_strong_buy_candle()] * 3, "BUY", window=3)
        self.assertFalse(fa.fade_detected)

    def test_exactly_enough_candles(self):
        from src.risk.candle_quality import evaluate_fade

        # window=3 needs exactly 4 candles
        candles = _shrinking_buy_sequence(3)  # 4 candles
        fa = evaluate_fade(candles, "BUY", window=3, confirmation_bars=1)
        # Should evaluate successfully (fade_detected may be True or False)
        self.assertEqual(len(fa.body_ratios), 3)


# ---------------------------------------------------------------------------
# Tests: evaluate_fade BUY — long trade sample behavior
# ---------------------------------------------------------------------------

class TestEvaluateFadeBuy(unittest.TestCase):
    """Verify a typical LONG momentum-fade scenario."""

    def _candles(self):
        """Four BUY candles with shrinking bodies: 0.80 → 0.55 → 0.35 → 0.15."""
        return _shrinking_buy_sequence(3)

    def test_fade_detected_buy(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(self._candles(), "BUY", window=3,
                           weak_body_threshold=0.40, confirmation_bars=1)
        # Last bar body = 0.15 < 0.40  → 1 weak bar → confirmation_bars=1 met
        self.assertTrue(fa.shrinking_body_sequence)
        self.assertTrue(fa.confirmation_bars_met)
        self.assertTrue(fa.fade_detected)

    def test_body_ratios_length_equals_window(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(self._candles(), "BUY", window=3, confirmation_bars=1)
        self.assertEqual(len(fa.body_ratios), 3)
        self.assertEqual(len(fa.wick_ratios_adverse), 3)

    def test_last_body_ratio_matches(self):
        from src.risk.candle_quality import evaluate_fade
        from src.risk.candle_quality import body_to_range_ratio

        candles = self._candles()
        fa = evaluate_fade(candles, "BUY", window=3, confirmation_bars=1)
        o, h, l, c = candles[-1]
        expected = body_to_range_ratio(o, h, l, c)
        self.assertAlmostEqual(fa.last_body_ratio, expected, places=6)

    def test_consecutive_strong_candles_zero_when_last_is_weak(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(self._candles(), "BUY", window=3, confirmation_bars=1)
        # Last candle body = 0.15 < 0.60 → not strong → consecutive = 0
        self.assertEqual(fa.consecutive_strong_candles, 0)

    def test_no_fade_when_bodies_not_shrinking(self):
        from src.risk.candle_quality import evaluate_fade

        # Flat strong candles — not shrinking
        candles = [_strong_buy_candle(0.75)] * 4
        fa = evaluate_fade(candles, "BUY", window=3, confirmation_bars=1)
        self.assertFalse(fa.shrinking_body_sequence)
        self.assertFalse(fa.fade_detected)

    def test_no_fade_when_confirmation_not_met(self):
        from src.risk.candle_quality import evaluate_fade

        # Shrinking sequence but last bar body = 0.45 (just above 0.40 threshold)
        candles = [_strong_buy_candle(b) for b in [0.80, 0.65, 0.55, 0.45]]
        fa = evaluate_fade(candles, "BUY", window=3,
                           weak_body_threshold=0.40, confirmation_bars=1)
        # body 0.45 > 0.40 → no weak candles → confirmation not met
        self.assertFalse(fa.confirmation_bars_met)
        self.assertFalse(fa.fade_detected)


# ---------------------------------------------------------------------------
# Tests: evaluate_fade SELL — short trade sample behavior
# ---------------------------------------------------------------------------

class TestEvaluateFadeSell(unittest.TestCase):
    """Verify a typical SHORT momentum-fade scenario."""

    def _sell_candle(self, body: float) -> tuple:
        # SELL: close < open  (range = 1.0, open = 0.90, close = 0.90 - body)
        return _candle(0.90, 1.0, 0.0, 0.90 - body)

    def _candles(self):
        return [self._sell_candle(b) for b in [0.80, 0.55, 0.35, 0.18]]

    def test_fade_detected_sell(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(self._candles(), "SELL", window=3,
                           weak_body_threshold=0.40, confirmation_bars=1)
        self.assertTrue(fa.fade_detected)

    def test_last_is_strong_false_for_weak_sell_candle(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(self._candles(), "SELL", window=3,
                           strong_body_threshold=0.60, confirmation_bars=1)
        # last body = 0.18 < 0.60 → not strong
        self.assertFalse(fa.last_is_strong)

    def test_consecutive_strong_from_tail(self):
        from src.risk.candle_quality import evaluate_fade

        # Two strong SELL candles followed by one weak
        candles = [
            self._sell_candle(0.70),   # baseline (not in eval window)
            self._sell_candle(0.75),   # strong
            self._sell_candle(0.78),   # strong
            self._sell_candle(0.20),   # weak last
        ]
        fa = evaluate_fade(candles, "SELL", window=3,
                           strong_body_threshold=0.60, confirmation_bars=1)
        # Consecutive strong from tail: last bar is weak → consecutive = 0
        self.assertEqual(fa.consecutive_strong_candles, 0)

    def test_no_fade_for_strong_sell_trend(self):
        from src.risk.candle_quality import evaluate_fade

        # Strong, consistent SELL candles (increasing momentum)
        candles = [self._sell_candle(0.65)] * 4
        fa = evaluate_fade(candles, "SELL", window=3, confirmation_bars=1)
        self.assertFalse(fa.fade_detected)


# ---------------------------------------------------------------------------
# Tests: confirmation_bars = 2 (SCALP policy — must NOT trigger on one weak bar)
# ---------------------------------------------------------------------------

class TestConfirmationBars(unittest.TestCase):
    """Core requirement: SCALP uses confirmation_bars=2 so a single doji
    inside a healthy move does NOT trigger candle-trail tightening."""

    def _mixed_candles(self):
        """Shrinking sequence but only ONE weak bar (body < 0.40).

        Bodies: 0.80, 0.55, 0.42, 0.30  — last bar 0.30 < 0.40, others >= 0.40
        Shrinking: True.  Weak count = 1.
        """
        return [_strong_buy_candle(b) for b in [0.80, 0.55, 0.42, 0.30]]

    def test_single_weak_bar_does_not_trigger_with_bars_2(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(
            self._mixed_candles(), "BUY", window=3,
            weak_body_threshold=0.40,
            confirmation_bars=2,   # SCALP setting
        )
        self.assertTrue(fa.shrinking_body_sequence)
        self.assertEqual(fa.weak_candles_in_window, 1)
        self.assertFalse(fa.confirmation_bars_met)
        self.assertFalse(fa.fade_detected)    # ← key assertion

    def test_single_weak_bar_DOES_trigger_with_bars_1(self):
        from src.risk.candle_quality import evaluate_fade

        fa = evaluate_fade(
            self._mixed_candles(), "BUY", window=3,
            weak_body_threshold=0.40,
            confirmation_bars=1,   # default / INTERMEDIATE setting
        )
        self.assertTrue(fa.confirmation_bars_met)
        self.assertTrue(fa.fade_detected)

    def test_two_weak_bars_trigger_with_bars_2(self):
        from src.risk.candle_quality import evaluate_fade

        # Two weak bars (< 0.40) in the evaluation window
        candles = [_strong_buy_candle(b) for b in [0.80, 0.55, 0.30, 0.18]]
        fa = evaluate_fade(candles, "BUY", window=3,
                           weak_body_threshold=0.40, confirmation_bars=2)
        self.assertEqual(fa.weak_candles_in_window, 2)
        self.assertTrue(fa.confirmation_bars_met)
        self.assertTrue(fa.fade_detected)


# ---------------------------------------------------------------------------
# Tests: ExitPolicy Phase 6 fields
# ---------------------------------------------------------------------------

class TestExitPolicyPhase6Fields(unittest.TestCase):
    def test_exit_policy_has_all_new_fields(self):
        from src.risk.exit_policies import ExitPolicy

        field_names = {f.name for f in dc_fields(ExitPolicy)}
        for fname in (
            "weak_body_threshold",
            "strong_body_threshold",
            "adverse_wick_threshold",
            "fade_confirmation_bars",
            "fade_tighten_frac",
        ):
            self.assertIn(fname, field_names, f"Missing field: {fname}")

    def test_exit_policy_defaults(self):
        from src.risk.exit_policies import ExitPolicy

        ep = ExitPolicy(name="TEST", giveback_frac=0.30, break_even_pct=0.50)
        self.assertAlmostEqual(ep.weak_body_threshold, 0.40)
        self.assertAlmostEqual(ep.strong_body_threshold, 0.60)
        self.assertAlmostEqual(ep.adverse_wick_threshold, 0.30)
        self.assertEqual(ep.fade_confirmation_bars, 1)
        self.assertAlmostEqual(ep.fade_tighten_frac, 0.30)

    def test_scalp_policy_confirmation_bars_is_2(self):
        from src.risk.exit_policies import ScalpExitPolicy

        self.assertEqual(ScalpExitPolicy.fade_confirmation_bars, 2)

    def test_scalp_policy_fade_tighten_frac(self):
        from src.risk.exit_policies import ScalpExitPolicy

        self.assertAlmostEqual(ScalpExitPolicy.fade_tighten_frac, 0.30)

    def test_intermediate_policy_uses_defaults(self):
        from src.risk.exit_policies import IntermediateExitPolicy

        self.assertEqual(IntermediateExitPolicy.fade_confirmation_bars, 1)

    def test_swing_policy_momentum_window_zero(self):
        from src.risk.exit_policies import SwingExitPolicy

        # SWING still has window=0 (no fade tightening)
        self.assertEqual(SwingExitPolicy.momentum_fade_window, 0)


# ---------------------------------------------------------------------------
# Tests: TradeRecord Phase 6 fields
# ---------------------------------------------------------------------------

class TestTradeRecordPhase6Fields(unittest.TestCase):
    def test_trade_record_has_fade_fields(self):
        from src.signals.types import TradeRecord

        field_names = {f.name for f in dc_fields(TradeRecord)}
        self.assertIn("fade_tighten_count", field_names)
        self.assertIn("last_fade_body_ratio", field_names)
        self.assertIn("last_fade_wick_ratio", field_names)

    def test_default_values(self):
        from src.signals.types import TradeRecord
        from datetime import datetime, timezone

        rec = TradeRecord(
            trade_id="t1", signal_type="BUY", asset="BTC-USD",
            timeframe="5m", entry_time=datetime.now(timezone.utc),
            entry_price=100.0, stop_loss_hard=95.0, trailing_stop=95.0,
            position_size=1.0, account_risk_pct=1.0,
            jaw_at_entry=90.0, teeth_at_entry=92.0, lips_at_entry=94.0,
            alligator_point=1, stochastic_point=1, vortex_point=1,
            ml_confidence=0.7, ai_confidence=0.6,
        )
        self.assertEqual(rec.fade_tighten_count, 0)
        self.assertIsNone(rec.last_fade_body_ratio)
        self.assertIsNone(rec.last_fade_wick_ratio)


# ---------------------------------------------------------------------------
# Tests: DB migration
# ---------------------------------------------------------------------------

class TestDbFadeMigration(unittest.TestCase):
    """Verify migrate_add_fade_observability_fields() creates the 3 columns."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def _make_trades_table(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trades ("
            "trade_id TEXT PRIMARY KEY, "
            "asset TEXT, status TEXT)"
        )
        conn.commit()

    def test_migration_adds_three_columns(self):
        from src.data import db as db_module
        from src.data.db import migrate_add_fade_observability_fields

        # Patch the DB path to our temp DB
        with patch.object(db_module, "_sqlite_path", return_value=self._db_path):
            conn = sqlite3.connect(self._db_path)
            try:
                self._make_trades_table(conn)
                conn.close()
                migrate_add_fade_observability_fields()
                conn2 = sqlite3.connect(self._db_path)
                cols = {row[1] for row in conn2.execute("PRAGMA table_info(trades)").fetchall()}
                conn2.close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        self.assertIn("fade_tighten_count", cols)
        self.assertIn("last_fade_body_ratio", cols)
        self.assertIn("last_fade_wick_ratio", cols)

    def test_migration_idempotent(self):
        """Calling migration twice should not raise."""
        from src.data import db as db_module
        from src.data.db import migrate_add_fade_observability_fields

        with patch.object(db_module, "_sqlite_path", return_value=self._db_path):
            conn = sqlite3.connect(self._db_path)
            self._make_trades_table(conn)
            conn.close()
            migrate_add_fade_observability_fields()
            migrate_add_fade_observability_fields()   # second call must not raise


# ---------------------------------------------------------------------------
# Tests: update_trade_lifecycle whitelist
# ---------------------------------------------------------------------------

class TestUpdateTradeLifecycleWhitelist(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def test_fade_fields_in_whitelist(self):
        """update_trade_lifecycle must accept all 3 new fade fields."""
        from src.data import db as db_module
        from src.data.db import update_trade_lifecycle

        with patch.object(db_module, "_sqlite_path", return_value=self._db_path):
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "CREATE TABLE trades ("
                "trade_id TEXT PRIMARY KEY, "
                "fade_tighten_count INTEGER DEFAULT 0, "
                "last_fade_body_ratio REAL, "
                "last_fade_wick_ratio REAL)"
            )
            conn.execute("INSERT INTO trades (trade_id) VALUES ('t42')")
            conn.commit()
            conn.close()

            # Should not raise; should write all 3 columns
            update_trade_lifecycle(
                "t42",
                fade_tighten_count=3,
                last_fade_body_ratio=0.28,
                last_fade_wick_ratio=0.12,
            )

            conn2 = sqlite3.connect(self._db_path)
            row = conn2.execute(
                "SELECT fade_tighten_count, last_fade_body_ratio, last_fade_wick_ratio"
                " FROM trades WHERE trade_id='t42'"
            ).fetchone()
            conn2.close()

        self.assertEqual(row[0], 3)
        self.assertAlmostEqual(row[1], 0.28, places=4)
        self.assertAlmostEqual(row[2], 0.12, places=4)


if __name__ == "__main__":
    unittest.main()
