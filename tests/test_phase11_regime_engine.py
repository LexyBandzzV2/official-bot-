"""Phase 11 tests — Regime Engine, persistence, gating hooks, and reporting.

Coverage:
  TestRegimeLabelHelpers      — enum helper methods (is_trending, is_chopp, etc.)
  TestClassifyLabel           — classify() label selection for trending/choppy/news/unknown
  TestClassifyConfidence      — confidence range, UNKNOWN→0.0, strong trend > 0.35
  TestShouldPersist           — change-based dedupe logic
  TestRegimePersistence       — DB round-trips, migrate idempotency, Supabase fallback
  TestRegimeGating            — resolve_ml/ai_threshold, resolve_position_size_factor
  TestRegimeContext           — is_confident, is_adverse, to_dict, defaults
  TestRegimeReporter          — JSON/markdown/terminal output shapes
  TestBackwardCompat          — TradeRecord defaults, signal types, NULL regime in DB trades
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

# ── imports under test ───────────────────────────────────────────────────────

from src.signals.regime_types import (
    RegimeLabel,
    RegimeContext,
    RegimeSnapshot,
    VolatilityMetrics,
    TrendMetrics,
    ChopMetrics,
)
from src.signals.regime_engine import classify, should_persist
from src.signals.regime_gating import (
    resolve_ml_threshold,
    resolve_ai_threshold,
    resolve_position_size_factor,
    build_regime_context_for_signal,
    populate_regime_modifiers,
)
from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord
from src.tools.regime_reporter import (
    get_regime_report_data,
    print_regime_report,
    regime_to_markdown,
    regime_to_json,
    _print_plain,
)
import src.data.db as db_mod


# ── Synthetic DataFrame factories ─────────────────────────────────────────────

def _make_trending_ha_df(n: int = 60):
    """Rising price, large solid bodies, consistent HH/HL → TRENDING classification.

    Design:
    * closes = linear rise from 100 → 160 (monotone)
    * bodies large  (open = close - 0.8, body/range ≈ 0.67 > 0.55)
    * small wicks   (range = 1.2)
    * Every bar is HH+HL → streak builds → abs(streak) >> 2 so reversal gate skips
    * breaks no breakout fails → breakout_fail_rate = 0
    """
    import pandas as pd
    import numpy as np

    closes = np.linspace(100.0, 160.0, n)
    opens  = closes - 0.8           # large bullish body
    highs  = closes + 0.2
    lows   = opens  - 0.2           # = closes - 1.0
    return pd.DataFrame({
        "ha_open":  opens,
        "ha_close": closes,
        "ha_high":  highs,
        "ha_low":   lows,
    })


def _make_choppy_ha_df(n: int = 60):
    """Range-bound, constant-ceiling, zigzag closes → CHOPPY classification.

    Design:
    * closes alternate 100/102 → high chop index
    * constant highs (103.0) → no breakout attempts → breakout_fail_rate=0
      → REVERSAL_TRANSITION gate condition 3 FAILS so code falls through to chop vote
    * tiny bodies (open ≈ close) → body_quality << 0.35 → amplifies chop vote
    * wide wicks (high - low ≈ 4-6) → high noise_ratio
    """
    import pandas as pd
    import numpy as np

    idx    = np.arange(n)
    closes = np.where(idx % 2 == 0, 100.0, 102.0).astype(float)
    opens  = np.where(idx % 2 == 0, 100.5, 101.5).astype(float)  # tiny body
    highs  = np.full(n, 103.0)                                     # constant ceiling
    lows   = np.where(idx % 2 == 0, 99.0, 97.0).astype(float)     # wide lower wick
    return pd.DataFrame({
        "ha_open":  opens,
        "ha_close": closes,
        "ha_high":  highs,
        "ha_low":   lows,
    })


def _make_news_volatile_ha_df(n: int = 60):
    """Stable bars then one massive expansion bar → NEWS_DRIVEN_UNSTABLE when flag=True.

    Design (for window=50 extracting last 50 of n=60):
    * bars 10-58 (49 bars): range=1.0, tiny body=0.1 → high noise_ratio
    * bar 59 (last): range=40 → atr_ratio = 40/1 = 40 >> 1.20
    * mean noise_ratio over window ≈ 0.897 >> 0.55
    """
    import pandas as pd
    import numpy as np

    closes = np.full(n, 100.0)
    opens  = np.full(n, 99.9)      # tiny body → high wick fraction
    highs  = np.full(n, 100.5)
    lows   = np.full(n, 99.5)
    # Last bar: massive expansion
    closes[-1] = 110.0
    opens[-1]  = 100.0
    highs[-1]  = 120.0
    lows[-1]   = 80.0
    return pd.DataFrame({
        "ha_open":  opens,
        "ha_close": closes,
        "ha_high":  highs,
        "ha_low":   lows,
    })


def _make_minimal_ha_df(n: int = 5):
    """Fewer than MIN_CANDLES (20) → classify() must return UNKNOWN."""
    import pandas as pd
    import numpy as np

    closes = np.linspace(100.0, 105.0, n)
    return pd.DataFrame({
        "ha_open":  closes - 0.5,
        "ha_close": closes,
        "ha_high":  closes + 0.5,
        "ha_low":   closes - 1.0,
    })


# ── Helper factories ───────────────────────────────────────────────────────────

def _make_regime_ctx(label: str = "CHOPPY_LOW_VOL", conf: float = 0.65) -> RegimeContext:
    """Build a RegimeContext with populated modifiers (skips DB/snapshot machinery)."""
    ctx = RegimeContext(
        regime_label     = RegimeLabel(label),
        confidence_score = conf,
        snapshot_id      = str(uuid.uuid4()),
    )
    populate_regime_modifiers(ctx)
    return ctx


def _make_snapshot(
    label: RegimeLabel = RegimeLabel.TRENDING_LOW_VOL,
    conf:  float       = 0.70,
    asset: str         = "BTCUSDT",
    tf:    str         = "5m",
) -> RegimeSnapshot:
    """Minimal RegimeSnapshot suitable for persistence round-trip tests."""
    return RegimeSnapshot(
        regime_id        = str(uuid.uuid4()),
        created_at       = datetime.now(timezone.utc),
        asset            = asset,
        asset_class      = "crypto",
        timeframe        = tf,
        strategy_mode    = "SCALP",
        regime_label     = label,
        confidence_score = conf,
        evidence_summary = f"[{label.value}  conf={conf:.2f}]  test_evidence",
    )


def _make_trade_record(**kwargs) -> TradeRecord:
    """Minimal valid TradeRecord for DB-insertion tests."""
    defaults = dict(
        trade_id         = str(uuid.uuid4()),
        signal_type      = "BUY",
        asset            = "BTCUSDT",
        timeframe        = "5m",
        entry_time       = datetime.now(timezone.utc),
        entry_price      = 100.0,
        stop_loss_hard   = 98.0,
        trailing_stop    = 98.0,
        position_size    = 1.0,
        account_risk_pct = 1.0,
        alligator_point  = True,
        stochastic_point = True,
        vortex_point     = True,
        jaw_at_entry     = 96.0,
        teeth_at_entry   = 98.0,
        lips_at_entry    = 100.5,
    )
    defaults.update(kwargs)
    return TradeRecord(**defaults)


# ── DB fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """Isolated SQLite at a temp path; Supabase disabled."""
    db_file = str(tmp_path / "test_regime.db")
    monkeypatch.setattr(db_mod, "SQLITE_PATH", db_file)
    db_mod._sb_client = None   # disable Supabase for all tests in this session
    db_mod.init_db()
    return db_mod


# ─────────────────────────────────────────────────────────────────────────────
# A. RegimeLabel enum helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeLabelHelpers:
    def test_trending_labels_report_is_trending(self):
        assert RegimeLabel.TRENDING_HIGH_VOL.is_trending() is True
        assert RegimeLabel.TRENDING_LOW_VOL.is_trending()  is True

    def test_choppy_labels_not_trending(self):
        assert RegimeLabel.CHOPPY_HIGH_VOL.is_trending() is False
        assert RegimeLabel.CHOPPY_LOW_VOL.is_trending()  is False

    def test_high_vol_labels(self):
        assert RegimeLabel.TRENDING_HIGH_VOL.is_high_vol() is True
        assert RegimeLabel.CHOPPY_HIGH_VOL.is_high_vol()   is True
        assert RegimeLabel.TRENDING_LOW_VOL.is_high_vol()  is False
        assert RegimeLabel.CHOPPY_LOW_VOL.is_high_vol()    is False

    def test_low_vol_labels(self):
        assert RegimeLabel.TRENDING_LOW_VOL.is_low_vol()  is True
        assert RegimeLabel.CHOPPY_LOW_VOL.is_low_vol()    is True
        assert RegimeLabel.TRENDING_HIGH_VOL.is_low_vol() is False

    def test_adverse_labels(self):
        assert RegimeLabel.CHOPPY_LOW_VOL.is_adverse()       is True
        assert RegimeLabel.NEWS_DRIVEN_UNSTABLE.is_adverse() is True
        assert RegimeLabel.REVERSAL_TRANSITION.is_adverse()  is True
        assert RegimeLabel.TRENDING_HIGH_VOL.is_adverse()    is False
        assert RegimeLabel.TRENDING_LOW_VOL.is_adverse()     is False
        assert RegimeLabel.CHOPPY_HIGH_VOL.is_adverse()      is False

    def test_unknown_is_unknown(self):
        assert RegimeLabel.UNKNOWN.is_unknown() is True
        for lbl in RegimeLabel:
            if lbl is not RegimeLabel.UNKNOWN:
                assert lbl.is_unknown() is False

    def test_label_value_strings(self):
        for lbl in RegimeLabel:
            assert isinstance(lbl.value, str)
            assert lbl.value == str(lbl.value)

    def test_all_seven_labels_exist(self):
        labels = {lbl.value for lbl in RegimeLabel}
        for expected in [
            "TRENDING_HIGH_VOL", "TRENDING_LOW_VOL",
            "CHOPPY_HIGH_VOL",   "CHOPPY_LOW_VOL",
            "REVERSAL_TRANSITION", "NEWS_DRIVEN_UNSTABLE", "UNKNOWN",
        ]:
            assert expected in labels


# ─────────────────────────────────────────────────────────────────────────────
# B. Classification — label selection
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyLabel:
    def test_trending_df_classified_as_trending(self):
        snap = classify(_make_trending_ha_df(), asset="BTC", timeframe="5m")
        assert snap.regime_label.is_trending(), (
            f"Expected a trending label, got {snap.regime_label}  "
            f"evidence: {snap.evidence_summary}"
        )

    def test_choppy_df_classified_as_choppy(self):
        snap = classify(_make_choppy_ha_df(), asset="BTC", timeframe="5m")
        assert snap.regime_label.is_choppy(), (
            f"Expected a choppy label, got {snap.regime_label}  "
            f"evidence: {snap.evidence_summary}"
        )

    def test_insufficient_candles_returns_unknown(self):
        snap = classify(_make_minimal_ha_df(n=5))
        assert snap.regime_label is RegimeLabel.UNKNOWN
        assert "insufficient" in snap.evidence_summary.lower()

    def test_none_df_returns_unknown(self):
        snap = classify(None)  # type: ignore[arg-type]
        assert snap.regime_label is RegimeLabel.UNKNOWN

    def test_news_flag_with_volatile_df_returns_news_unstable(self):
        snap = classify(_make_news_volatile_ha_df(), news_instability_flag=True)
        assert snap.regime_label is RegimeLabel.NEWS_DRIVEN_UNSTABLE, (
            f"Expected NEWS_DRIVEN_UNSTABLE, got {snap.regime_label}  "
            f"evidence: {snap.evidence_summary}"
        )

    def test_news_flag_false_does_not_produce_news_label(self):
        snap = classify(_make_news_volatile_ha_df(), news_instability_flag=False)
        assert snap.regime_label is not RegimeLabel.NEWS_DRIVEN_UNSTABLE

    def test_classify_returns_regime_snapshot_instance(self):
        snap = classify(_make_trending_ha_df())
        assert isinstance(snap, RegimeSnapshot)

    def test_classify_deterministic_same_inputs_same_output(self):
        df = _make_trending_ha_df(n=60)
        s1 = classify(df, asset="BTC", timeframe="5m")
        s2 = classify(df, asset="BTC", timeframe="5m")
        assert s1.regime_label     == s2.regime_label
        assert s1.confidence_score == s2.confidence_score

    def test_classify_attaches_asset_metadata(self):
        snap = classify(_make_trending_ha_df(), asset="ETHUSDT", timeframe="15m")
        assert snap.asset     == "ETHUSDT"
        assert snap.timeframe == "15m"

    def test_classify_evidence_summary_non_empty_string(self):
        snap = classify(_make_trending_ha_df())
        assert isinstance(snap.evidence_summary, str)
        assert len(snap.evidence_summary) > 5

    def test_classify_produces_unique_regime_ids(self):
        s1 = classify(_make_trending_ha_df())
        s2 = classify(_make_trending_ha_df())
        assert s1.regime_id != s2.regime_id

    def test_classify_boundary_exactly_20_candles(self):
        """Exactly MIN_CANDLES rows must not return UNKNOWN for insufficient data."""
        import pandas as pd
        import numpy as np
        n = 20
        closes = np.linspace(100.0, 120.0, n)
        df = pd.DataFrame({
            "ha_open":  closes - 0.5,
            "ha_close": closes,
            "ha_high":  closes + 0.5,
            "ha_low":   closes - 1.0,
        })
        snap = classify(df)
        # With exactly 20 bars the classification may be any label, but not UNKNOWN
        # due to insufficient_candles check (it fires only at n < 20)
        assert "insufficient_candles" not in snap.evidence_summary


# ─────────────────────────────────────────────────────────────────────────────
# C. Classification — confidence scoring
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyConfidence:
    def test_confidence_in_unit_range_for_all_fixtures(self):
        for factory in [_make_trending_ha_df, _make_choppy_ha_df, _make_minimal_ha_df]:
            snap = classify(factory())
            assert 0.0 <= snap.confidence_score <= 1.0, (
                f"{factory.__name__}: confidence {snap.confidence_score} out of [0,1]"
            )

    def test_unknown_label_has_zero_confidence(self):
        snap = classify(_make_minimal_ha_df(n=5))
        assert snap.regime_label is RegimeLabel.UNKNOWN
        assert snap.confidence_score == 0.0

    def test_none_df_has_zero_confidence(self):
        snap = classify(None)  # type: ignore[arg-type]
        assert snap.confidence_score == 0.0

    def test_strong_trend_confidence_above_minimum(self):
        snap = classify(_make_trending_ha_df(n=60))
        if snap.regime_label.is_trending():
            assert snap.confidence_score >= 0.35, (
                f"Expected conf >= 0.35 for strong trend, got {snap.confidence_score}"
            )

    def test_confidence_is_float(self):
        snap = classify(_make_trending_ha_df())
        assert isinstance(snap.confidence_score, float)

    def test_news_unstable_has_positive_confidence(self):
        snap = classify(_make_news_volatile_ha_df(), news_instability_flag=True)
        assert snap.confidence_score > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# D. should_persist decision logic
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldPersist:
    def test_first_snapshot_always_persists(self):
        assert should_persist(_make_snapshot(), prev_snapshot=None) is True

    def test_same_label_small_delta_does_not_persist(self):
        prev = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.70)
        new  = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.72)
        # delta = 0.02 < 0.15
        assert should_persist(new, prev_snapshot=prev) is False

    def test_label_change_triggers_persist(self):
        prev = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.70)
        new  = _make_snapshot(label=RegimeLabel.CHOPPY_HIGH_VOL,  conf=0.70)
        assert should_persist(new, prev_snapshot=prev) is True

    def test_large_confidence_delta_triggers_persist(self):
        prev = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.50)
        new  = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.70)
        # delta = 0.20 >= REGIME_CHANGE_CONFIDENCE_DELTA (0.15)
        assert should_persist(new, prev_snapshot=prev) is True

    def test_small_confidence_delta_does_not_persist(self):
        prev = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.60)
        new  = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.65)
        # delta = 0.05 < 0.15
        assert should_persist(new, prev_snapshot=prev) is False

    def test_unknown_to_known_triggers_persist(self):
        prev = _make_snapshot(label=RegimeLabel.UNKNOWN, conf=0.0)
        new  = _make_snapshot(label=RegimeLabel.TRENDING_HIGH_VOL, conf=0.70)
        assert should_persist(new, prev_snapshot=prev) is True

    def test_confidence_delta_exactly_at_boundary_triggers_persist(self):
        """delta exactly == REGIME_CHANGE_CONFIDENCE_DELTA should persist (>= check)."""
        prev = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.50)
        new  = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.65)
        # delta = 0.15 — ties go to persist
        assert should_persist(new, prev_snapshot=prev) is True

    def test_both_unknown_does_not_persist(self):
        prev = _make_snapshot(label=RegimeLabel.UNKNOWN, conf=0.0)
        new  = _make_snapshot(label=RegimeLabel.UNKNOWN, conf=0.0)
        # Same label (UNKNOWN == UNKNOWN), delta = 0 → no persist
        assert should_persist(new, prev_snapshot=prev) is False

    def test_reversal_to_trending_triggers_persist(self):
        prev = _make_snapshot(label=RegimeLabel.REVERSAL_TRANSITION, conf=0.65)
        new  = _make_snapshot(label=RegimeLabel.TRENDING_HIGH_VOL,   conf=0.65)
        assert should_persist(new, prev_snapshot=prev) is True


# ─────────────────────────────────────────────────────────────────────────────
# E. DB persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimePersistence:
    def test_migrate_is_idempotent(self, _db):
        """Calling migration multiple times must not raise."""
        _db.migrate_add_regime_snapshots_table()
        _db.migrate_add_regime_snapshots_table()

    def test_regime_snapshots_table_created_after_init(self, _db):
        with _db._sqlite_conn() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "regime_snapshots" in tables

    def test_trades_has_regime_columns_after_migration(self, _db):
        with _db._sqlite_conn() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        assert "regime_label_at_entry"      in cols
        assert "regime_confidence_at_entry" in cols
        assert "regime_snapshot_id"          in cols

    def test_save_and_retrieve_round_trip(self, _db):
        snap = _make_snapshot(asset="BTCUSDT", tf="5m")
        _db.save_regime_snapshot(snap)
        result = _db.get_latest_regime_snapshot("BTCUSDT", "5m")
        assert result is not None
        assert result["regime_id"]    == snap.regime_id
        assert result["regime_label"] == snap.regime_label.value
        assert abs(result["confidence_score"] - snap.confidence_score) < 1e-6

    def test_get_regime_snapshots_returns_list_of_dicts(self, _db):
        for _ in range(3):
            _db.save_regime_snapshot(_make_snapshot(asset="ETHUSDT", tf="15m"))
        rows = _db.get_regime_snapshots(asset="ETHUSDT", timeframe="15m")
        assert isinstance(rows, list)
        assert len(rows) == 3
        assert all(isinstance(r, dict) for r in rows)

    def test_get_regime_snapshots_filters_by_asset(self, _db):
        _db.save_regime_snapshot(_make_snapshot(asset="BTCUSDT", tf="5m"))
        _db.save_regime_snapshot(_make_snapshot(asset="ETHUSDT", tf="5m"))
        btc_rows = _db.get_regime_snapshots(asset="BTCUSDT")
        assert len(btc_rows) >= 1
        assert all(r["asset"] == "BTCUSDT" for r in btc_rows)

    def test_get_regime_snapshots_filters_by_timeframe(self, _db):
        _db.save_regime_snapshot(_make_snapshot(asset="BTCUSDT", tf="5m"))
        _db.save_regime_snapshot(_make_snapshot(asset="BTCUSDT", tf="1h"))
        rows_5m = _db.get_regime_snapshots(timeframe="5m")
        assert len(rows_5m) >= 1
        assert all(r["timeframe"] == "5m" for r in rows_5m)

    def test_get_regime_snapshots_empty_db_returns_empty_list(self, _db):
        rows = _db.get_regime_snapshots(asset="NONEXISTENT_ASSET")
        assert rows == []

    def test_get_latest_returns_most_recent_snapshot(self, _db):
        import time
        s1 = _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, conf=0.55,
                            asset="BTCUSDT", tf="5m")
        _db.save_regime_snapshot(s1)
        time.sleep(0.02)
        s2 = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.65,
                            asset="BTCUSDT", tf="5m")
        _db.save_regime_snapshot(s2)
        latest = _db.get_latest_regime_snapshot("BTCUSDT", "5m")
        assert latest is not None
        assert latest["regime_id"] == s2.regime_id

    def test_get_latest_returns_none_for_missing_asset(self, _db):
        result = _db.get_latest_regime_snapshot("ZZZUNKNOWN", "99m")
        assert result is None

    def test_save_snapshot_supabase_failure_does_not_raise(self, _db, monkeypatch):
        """Supabase network failure must not propagate — SQLite row must still exist."""
        class _FakeSB:
            def table(self, *a, **kw):
                raise RuntimeError("simulated network failure")

        monkeypatch.setattr(db_mod, "_get_supabase", lambda: _FakeSB())
        snap = _make_snapshot(asset="SNPTEST", tf="1m")
        _db.save_regime_snapshot(snap)   # must NOT raise
        result = _db.get_latest_regime_snapshot("SNPTEST", "1m")
        assert result is not None
        assert result["regime_id"] == snap.regime_id

    def test_duplicate_regime_id_uses_upsert_not_error(self, _db):
        """INSERT OR REPLACE: same regime_id saved twice → still only one row."""
        snap = _make_snapshot(asset="BTCUSDT", tf="5m")
        _db.save_regime_snapshot(snap)
        _db.save_regime_snapshot(snap)   # second write with identical regime_id
        rows = _db.get_regime_snapshots(asset="BTCUSDT", timeframe="5m")
        assert len(rows) == 1

    def test_snapshot_evidence_summary_saved_and_retrieved(self, _db):
        snap = _make_snapshot()
        snap.evidence_summary = "custom_evidence_text"
        _db.save_regime_snapshot(snap)
        row = _db.get_latest_regime_snapshot(snap.asset, snap.timeframe)
        assert row is not None
        assert row["evidence_summary"] == "custom_evidence_text"

    def test_news_flag_persisted_as_integer(self, _db):
        snap = _make_snapshot()
        snap.news_instability_flag = True
        _db.save_regime_snapshot(snap)
        row = _db.get_latest_regime_snapshot(snap.asset, snap.timeframe)
        assert row is not None
        assert row["news_instability_flag"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# F. Regime-aware threshold and size gating
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeGating:
    # ── resolve_ml_threshold ─────────────────────────────────────────────────

    def test_ml_unchanged_when_no_context(self):
        assert resolve_ml_threshold(0.65, None) == 0.65

    def test_ml_unchanged_for_unknown_label(self):
        ctx = RegimeContext(regime_label=RegimeLabel.UNKNOWN, confidence_score=0.90)
        assert resolve_ml_threshold(0.65, ctx) == 0.65

    def test_ml_unchanged_when_confidence_below_minimum(self):
        ctx = _make_regime_ctx("CHOPPY_LOW_VOL", conf=0.20)
        # conf 0.20 < REGIME_MIN_CONFIDENCE (0.40) → fail-open
        assert resolve_ml_threshold(0.65, ctx) == 0.65

    def test_ml_raised_for_choppy_low_vol(self):
        ctx = _make_regime_ctx("CHOPPY_LOW_VOL", conf=0.65)
        result = resolve_ml_threshold(0.65, ctx)
        # CHOPPY_LOW_VOL_ML_DELTA = +0.05 → 0.65 + 0.05 = 0.70
        assert result == pytest.approx(0.70, abs=0.001)

    def test_ml_raised_for_choppy_high_vol(self):
        ctx = _make_regime_ctx("CHOPPY_HIGH_VOL", conf=0.65)
        result = resolve_ml_threshold(0.65, ctx)
        # CHOPPY_HIGH_VOL_ML_DELTA = +0.07 → 0.72
        assert result == pytest.approx(0.72, abs=0.001)

    def test_ml_raised_for_news_unstable(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.65)
        result = resolve_ml_threshold(0.65, ctx)
        # NEWS_UNSTABLE_ML_DELTA = +0.10 → 0.75
        assert result == pytest.approx(0.75, abs=0.001)

    def test_ml_lowered_for_trending_high_vol(self):
        ctx = _make_regime_ctx("TRENDING_HIGH_VOL", conf=0.65)
        result = resolve_ml_threshold(0.65, ctx)
        # TRENDING_HIGH_VOL_ML_DELTA = -0.03 → 0.62
        assert result == pytest.approx(0.62, abs=0.001)

    def test_ml_unchanged_for_trending_low_vol(self):
        ctx = _make_regime_ctx("TRENDING_LOW_VOL", conf=0.65)
        result = resolve_ml_threshold(0.65, ctx)
        # TRENDING_LOW_VOL_ML_DELTA = 0.0 → no change
        assert result == pytest.approx(0.65, abs=0.001)

    def test_ml_result_clamped_to_max(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.65)
        # Even with large base the result stays ≤ REGIME_ML_THRESHOLD_MAX
        result = resolve_ml_threshold(0.88, ctx)
        assert result <= 0.90

    def test_ml_result_clamped_to_min(self):
        ctx = _make_regime_ctx("TRENDING_HIGH_VOL", conf=0.65)
        # delta = -0.03; base=0.46 → raw=0.43 → clamped up to 0.45
        result = resolve_ml_threshold(0.46, ctx)
        assert result >= 0.45

    def test_ml_returns_float(self):
        assert isinstance(resolve_ml_threshold(0.65, None), float)

    # ── resolve_ai_threshold ─────────────────────────────────────────────────

    def test_ai_unchanged_when_no_context(self):
        assert resolve_ai_threshold(0.60, None) == 0.60

    def test_ai_unchanged_for_unknown_label(self):
        ctx = RegimeContext(regime_label=RegimeLabel.UNKNOWN, confidence_score=0.90)
        assert resolve_ai_threshold(0.60, ctx) == 0.60

    def test_ai_raised_for_news_unstable(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.65)
        result = resolve_ai_threshold(0.60, ctx)
        # NEWS_UNSTABLE_AI_DELTA = +0.10 → 0.70
        assert result == pytest.approx(0.70, abs=0.001)

    def test_ai_raised_for_reversal_transition(self):
        ctx = _make_regime_ctx("REVERSAL_TRANSITION", conf=0.65)
        result = resolve_ai_threshold(0.60, ctx)
        # REVERSAL_AI_DELTA = +0.05 → 0.65
        assert result == pytest.approx(0.65, abs=0.001)

    def test_ai_unchanged_below_min_confidence(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.10)
        assert resolve_ai_threshold(0.60, ctx) == 0.60

    # ── resolve_position_size_factor ─────────────────────────────────────────

    def test_size_factor_one_when_no_context(self):
        assert resolve_position_size_factor(None) == pytest.approx(1.0)

    def test_size_factor_one_for_unknown_label(self):
        ctx = RegimeContext(regime_label=RegimeLabel.UNKNOWN, confidence_score=0.90)
        assert resolve_position_size_factor(ctx) == pytest.approx(1.0)

    def test_size_factor_one_below_min_confidence(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.10)
        assert resolve_position_size_factor(ctx) == pytest.approx(1.0)

    def test_size_factor_reduced_for_news_unstable(self):
        ctx = _make_regime_ctx("NEWS_DRIVEN_UNSTABLE", conf=0.65)
        # NEWS_UNSTABLE_SIZE_FACTOR = 0.50
        assert resolve_position_size_factor(ctx) == pytest.approx(0.50, abs=0.01)

    def test_size_factor_reduced_for_choppy_high_vol(self):
        ctx = _make_regime_ctx("CHOPPY_HIGH_VOL", conf=0.65)
        # CHOPPY_HIGH_VOL_SIZE_FACTOR = 0.65
        assert resolve_position_size_factor(ctx) == pytest.approx(0.65, abs=0.01)

    def test_size_factor_reduced_for_choppy_low_vol(self):
        ctx = _make_regime_ctx("CHOPPY_LOW_VOL", conf=0.65)
        # CHOPPY_LOW_VOL_SIZE_FACTOR = 0.75
        assert resolve_position_size_factor(ctx) == pytest.approx(0.75, abs=0.01)

    def test_size_factor_increased_for_trending_high_vol(self):
        ctx = _make_regime_ctx("TRENDING_HIGH_VOL", conf=0.65)
        # TRENDING_HIGH_VOL_SIZE_FACTOR = 1.10
        assert resolve_position_size_factor(ctx) == pytest.approx(1.10, abs=0.01)

    def test_size_factor_unchanged_for_trending_low_vol(self):
        ctx = _make_regime_ctx("TRENDING_LOW_VOL", conf=0.65)
        # TRENDING_LOW_VOL_SIZE_FACTOR = 1.0
        assert resolve_position_size_factor(ctx) == pytest.approx(1.0, abs=0.01)

    def test_size_factor_within_clamp_bounds(self):
        for label in ["TRENDING_HIGH_VOL", "TRENDING_LOW_VOL", "CHOPPY_HIGH_VOL",
                      "CHOPPY_LOW_VOL", "NEWS_DRIVEN_UNSTABLE", "REVERSAL_TRANSITION"]:
            ctx = _make_regime_ctx(label, conf=0.65)
            result = resolve_position_size_factor(ctx)
            assert 0.40 <= result <= 1.25, (
                f"{label}: size_factor {result} outside [0.40, 1.25]"
            )

    # ── build_regime_context_for_signal ─────────────────────────────────────

    def test_build_context_label_matches_snapshot(self):
        snap = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.70)
        ctx  = build_regime_context_for_signal(snap)
        assert ctx is not None
        assert ctx.regime_label == RegimeLabel.CHOPPY_LOW_VOL

    def test_build_context_confidence_matches_snapshot(self):
        snap = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.70)
        ctx  = build_regime_context_for_signal(snap)
        assert abs(ctx.confidence_score - 0.70) < 1e-6

    def test_build_context_populates_ml_modifier(self):
        snap = _make_snapshot(label=RegimeLabel.CHOPPY_LOW_VOL, conf=0.70)
        ctx  = build_regime_context_for_signal(snap)
        # CHOPPY_LOW_VOL has positive ML delta
        assert ctx.ml_threshold_delta > 0

    def test_build_context_populates_size_factor_below_one_for_adverse(self):
        snap = _make_snapshot(label=RegimeLabel.NEWS_DRIVEN_UNSTABLE, conf=0.70)
        ctx  = build_regime_context_for_signal(snap)
        assert ctx.position_size_factor < 1.0

    def test_build_context_snapshot_id_linked(self):
        snap = _make_snapshot()
        ctx  = build_regime_context_for_signal(snap)
        assert ctx.snapshot_id == snap.regime_id


# ─────────────────────────────────────────────────────────────────────────────
# G. RegimeContext dataclass methods
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeContext:
    def test_is_confident_true_for_known_label_above_threshold(self):
        ctx = RegimeContext(regime_label=RegimeLabel.TRENDING_LOW_VOL,
                           confidence_score=0.60)
        assert ctx.is_confident(min_confidence=0.40) is True

    def test_is_confident_false_for_unknown_label(self):
        ctx = RegimeContext(regime_label=RegimeLabel.UNKNOWN, confidence_score=0.95)
        assert ctx.is_confident() is False

    def test_is_confident_false_for_none_label(self):
        ctx = RegimeContext(regime_label=None, confidence_score=0.95)
        assert ctx.is_confident() is False

    def test_is_confident_false_below_min_confidence(self):
        ctx = RegimeContext(regime_label=RegimeLabel.TRENDING_LOW_VOL,
                           confidence_score=0.30)
        assert ctx.is_confident(min_confidence=0.40) is False

    def test_is_adverse_true_for_adverse_labels(self):
        for label in [
            RegimeLabel.CHOPPY_LOW_VOL,
            RegimeLabel.NEWS_DRIVEN_UNSTABLE,
            RegimeLabel.REVERSAL_TRANSITION,
        ]:
            ctx = RegimeContext(regime_label=label, confidence_score=0.60)
            assert ctx.is_adverse() is True, f"{label} should be adverse"

    def test_is_adverse_false_for_non_adverse_labels(self):
        for label in [
            RegimeLabel.TRENDING_HIGH_VOL,
            RegimeLabel.TRENDING_LOW_VOL,
            RegimeLabel.CHOPPY_HIGH_VOL,
        ]:
            ctx = RegimeContext(regime_label=label, confidence_score=0.70)
            assert ctx.is_adverse() is False, f"{label} should not be adverse"

    def test_is_adverse_false_for_low_confidence_adverse_label(self):
        """Adverse label below min_confidence → not considered adverse (fail-open)."""
        ctx = RegimeContext(regime_label=RegimeLabel.CHOPPY_LOW_VOL,
                           confidence_score=0.10)
        assert ctx.is_adverse() is False

    def test_to_dict_has_required_keys(self):
        ctx = RegimeContext(regime_label=RegimeLabel.CHOPPY_HIGH_VOL,
                           confidence_score=0.55)
        d = ctx.to_dict()
        for k in [
            "regime_label", "confidence_score", "evidence_summary",
            "ml_threshold_delta", "ai_threshold_delta", "position_size_factor",
        ]:
            assert k in d, f"Missing key: {k}"

    def test_to_dict_regime_label_is_string(self):
        ctx = RegimeContext(regime_label=RegimeLabel.CHOPPY_HIGH_VOL,
                           confidence_score=0.55)
        assert isinstance(ctx.to_dict()["regime_label"], str)

    def test_to_log_str_contains_label_and_confidence(self):
        ctx = RegimeContext(regime_label=RegimeLabel.TRENDING_LOW_VOL,
                           confidence_score=0.70)
        log_str = ctx.to_log_str()
        assert "TRENDING_LOW_VOL" in log_str
        assert "0.70" in log_str

    def test_default_context_is_completely_fail_open(self):
        ctx = RegimeContext()
        assert ctx.ml_threshold_delta   == 0.0
        assert ctx.ai_threshold_delta   == 0.0
        assert ctx.position_size_factor == 1.0
        assert ctx.score_bias           == 0.0
        assert ctx.regime_label         is None
        assert ctx.snapshot_id          is None

    def test_context_with_unknown_label_is_not_adverse(self):
        ctx = RegimeContext(regime_label=RegimeLabel.UNKNOWN, confidence_score=0.80)
        assert ctx.is_adverse() is False
        assert ctx.is_confident() is False


# ─────────────────────────────────────────────────────────────────────────────
# H. Regime reporter output
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeReporter:
    def test_get_data_returns_dict_with_required_keys(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        for k in [
            "generated_at", "total_closed_trades", "total_regime_snapshots",
            "regime_stats", "mode_regime_stats", "snapshot_distribution",
            "threshold_diagnostics", "conclusions",
        ]:
            assert k in data, f"Missing top-level key: {k}"

    def test_get_data_zero_counts_on_empty_db(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        assert data["total_closed_trades"]   == 0
        assert data["total_regime_snapshots"] == 0

    def test_conclusions_none_with_no_trades(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        conc = data["conclusions"]
        assert conc.get("best_regime_by_pnl")  is None
        assert conc.get("worst_regime_by_pnl") is None

    def test_regime_stats_has_all_seven_labels(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        stats = data["regime_stats"]
        for label in [
            "TRENDING_HIGH_VOL", "TRENDING_LOW_VOL",
            "CHOPPY_HIGH_VOL",   "CHOPPY_LOW_VOL",
            "REVERSAL_TRANSITION", "NEWS_DRIVEN_UNSTABLE", "UNKNOWN",
        ]:
            assert label in stats, f"Missing regime label in regime_stats: {label}"

    def test_snapshot_distribution_captured(self, _db, tmp_path):
        # Save two snapshots for the same asset/timeframe
        _db.save_regime_snapshot(
            _make_snapshot(label=RegimeLabel.TRENDING_LOW_VOL, asset="BTC", tf="5m")
        )
        _db.save_regime_snapshot(
            _make_snapshot(label=RegimeLabel.CHOPPY_HIGH_VOL, asset="BTC", tf="5m")
        )
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        assert data["total_regime_snapshots"] == 2
        snap_dist = data["snapshot_distribution"]
        assert len(snap_dist) >= 2  # at least two distinct labels recorded

    def test_regime_to_json_is_valid_json(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        raw = regime_to_json(data)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_regime_to_json_has_required_keys(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        parsed = json.loads(regime_to_json(data))
        for k in [
            "generated_at", "total_closed_trades",
            "regime_stats", "mode_regime_stats", "conclusions",
        ]:
            assert k in parsed, f"Missing JSON key: {k}"

    def test_regime_to_json_floats_at_most_4dp(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        parsed = json.loads(regime_to_json(data))

        def _check(obj: object) -> None:
            if isinstance(obj, float):
                assert obj == round(obj, 4), f"Float {obj!r} exceeds 4dp"
            elif isinstance(obj, dict):
                for v in obj.values():
                    _check(v)
            elif isinstance(obj, list):
                for v in obj:
                    _check(v)

        _check(parsed)

    def test_regime_to_markdown_contains_heading(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        md = regime_to_markdown(data)
        assert "# Regime Performance Report" in md

    def test_regime_to_markdown_contains_conclusions_section(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        md = regime_to_markdown(data)
        assert "## Conclusions" in md

    def test_regime_to_markdown_contains_outcome_stats_section(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        md = regime_to_markdown(data)
        assert "## Regime Outcome Stats" in md

    def test_print_plain_produces_output(self, _db, tmp_path, capsys):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        _print_plain(data)
        captured = capsys.readouterr()
        assert len(captured.out) > 0
        assert "REGIME PERFORMANCE REPORT" in captured.out

    def test_print_regime_report_runs_without_error(self, _db, tmp_path):
        from io import StringIO
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        try:
            from rich.console import Console
            buf = StringIO()
            con = Console(file=buf, width=160, force_terminal=True)
            print_regime_report(data, console=con)
            assert len(buf.getvalue()) > 0
        except ImportError:
            # Fall through to plain print (no rich installed)
            print_regime_report(data, console=None)

    def test_mode_regime_stats_has_all_three_modes(self, _db, tmp_path):
        data = get_regime_report_data(db_path=str(tmp_path / "test_regime.db"))
        mode_stats = data["mode_regime_stats"]
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert mode in mode_stats, f"Missing strategy mode: {mode}"


# ─────────────────────────────────────────────────────────────────────────────
# I. Backward compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_trade_record_has_regime_fields_with_correct_defaults(self):
        rec = _make_trade_record()
        assert hasattr(rec, "regime_label_at_entry")
        assert hasattr(rec, "regime_confidence_at_entry")
        assert hasattr(rec, "regime_snapshot_id")
        assert rec.regime_label_at_entry      is None
        assert rec.regime_confidence_at_entry == 0.0
        assert rec.regime_snapshot_id         is None

    def test_trade_record_regime_fields_accept_values(self):
        rec = _make_trade_record(
            regime_label_at_entry     = "TRENDING_LOW_VOL",
            regime_confidence_at_entry= 0.75,
            regime_snapshot_id        = str(uuid.uuid4()),
        )
        assert rec.regime_label_at_entry      == "TRENDING_LOW_VOL"
        assert rec.regime_confidence_at_entry == pytest.approx(0.75)
        assert rec.regime_snapshot_id         is not None

    def test_buy_signal_result_regime_context_defaults_to_none(self):
        sig = BuySignalResult()
        assert sig.regime_context is None

    def test_sell_signal_result_regime_context_defaults_to_none(self):
        sig = SellSignalResult()
        assert sig.regime_context is None

    def test_buy_signal_result_accepts_regime_context_object(self):
        ctx = RegimeContext(regime_label=RegimeLabel.TRENDING_LOW_VOL,
                           confidence_score=0.60)
        sig = BuySignalResult(regime_context=ctx)
        assert sig.regime_context is ctx

    def test_sell_signal_result_accepts_regime_context_object(self):
        ctx = RegimeContext(regime_label=RegimeLabel.CHOPPY_HIGH_VOL,
                           confidence_score=0.55)
        sig = SellSignalResult(regime_context=ctx)
        assert sig.regime_context is ctx

    def test_reporter_handles_closed_trades_without_regime_label(self, _db, tmp_path):
        """Trades with NULL regime_label_at_entry (pre-Phase-11 rows) must not crash."""
        db_file = str(tmp_path / "test_regime.db")
        # Insert a closed trade without regime fields; they default to NULL
        with _db._sqlite_conn() as conn:
            conn.execute(
                """INSERT INTO trades
                   (trade_id, signal_type, asset, timeframe, entry_time, entry_price,
                    stop_loss_hard, trailing_stop, position_size, account_risk_pct,
                    status, strategy_mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), "BUY", "BTCUSDT", "5m",
                    "2024-01-01T00:00:00", 100.0,
                    98.0, 98.0, 1.0, 1.0,
                    "CLOSED", "SCALP",
                ),
            )
        data = get_regime_report_data(db_path=db_file)
        assert data["total_closed_trades"] == 1     # row was found
        assert isinstance(data["regime_stats"], dict)  # no crash

    def test_save_trade_open_with_regime_fields_persists_them(self, _db):
        """save_trade_open must persist regime_label_at_entry when provided."""
        snap_id = str(uuid.uuid4())
        rec = _make_trade_record(
            regime_label_at_entry      = "CHOPPY_HIGH_VOL",
            regime_confidence_at_entry = 0.72,
            regime_snapshot_id         = snap_id,
        )
        _db.save_trade_open(rec)
        with _db._sqlite_conn() as conn:
            row = dict(conn.execute(
                "SELECT regime_label_at_entry, regime_confidence_at_entry, regime_snapshot_id "
                "FROM trades WHERE trade_id=?",
                (rec.trade_id,),
            ).fetchone())
        assert row["regime_label_at_entry"]       == "CHOPPY_HIGH_VOL"
        assert abs(row["regime_confidence_at_entry"] - 0.72) < 1e-6
        assert row["regime_snapshot_id"]           == snap_id

    def test_save_trade_open_without_regime_fields_does_not_crash(self, _db):
        """Saving a TradeRecord with all-default regime fields must not raise."""
        rec = _make_trade_record()   # no regime fields supplied → all defaults
        _db.save_trade_open(rec)     # must not raise

    def test_existing_trade_fields_unaffected_by_phase11(self):
        """Phase 1–10 TradeRecord fields still construct and default correctly."""
        rec = _make_trade_record()
        assert rec.status         == "OPEN"
        assert rec.strategy_mode  == "UNKNOWN"
        assert rec.pnl            == 0.0
        assert rec.close_reason   is None
        assert rec.entry_reason   is None

    def test_regime_context_none_does_not_break_gating_functions(self):
        """All gating functions must return base values when context is None."""
        assert resolve_ml_threshold(0.65, None)       == 0.65
        assert resolve_ai_threshold(0.60, None)       == 0.60
        assert resolve_position_size_factor(None)     == pytest.approx(1.0)
