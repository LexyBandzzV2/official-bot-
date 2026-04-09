"""Final Sprint tests — asset universe, prefilters, funnel reporter, and DB migration.

Coverage:
  TestAssetUniverse           — registry, group queries, enable/disable, filter_to_universe
  TestPrefilterVolatility     — ATR% gate per mode
  TestPrefilterVolume         — volume expansion gate
  TestPrefilterMemeLane       — stricter meme coin checks
  TestPrefilterPipeline       — full run_prefilter + select_top_candidates
  TestPrefilterQualityFirst   — quality-first mode raises thresholds
  TestFunnelReporter          — build_funnel_data, terminal, markdown, JSON
  TestSignalPrefilterFields   — BuySignalResult / SellSignalResult prefilter audit fields
  TestDBMigration             — migrate_add_prefilter_columns + _signal_row roundtrip
  TestConfigKnobs             — all Final Sprint config constants exist and have defaults
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Imports under test ────────────────────────────────────────────────────────

from src.scanner.asset_universe import (
    UniverseGroup,
    AssetEntry,
    get_entry,
    is_known,
    is_meme,
    universe_group,
    asset_class,
    is_group_enabled,
    get_enabled_symbols,
    get_symbols_for_group,
    filter_to_universe,
    all_groups,
    registry_snapshot,
)
from src.scanner.prefilters import (
    PrefilterResult,
    check_volatility,
    check_volume,
    check_meme_lane,
    compute_rank_score,
    run_prefilter,
    select_top_candidates,
    SKIP_LOW_VOLATILITY,
    SKIP_WEAK_VOLUME,
    SKIP_MEME_LOW_VOLATILITY,
    SKIP_MEME_WEAK_VOLUME,
    SKIP_MEME_LOW_LIQUIDITY,
    SKIP_BELOW_RANK_CUTOFF,
)
from src.scanner.funnel_reporter import (
    build_funnel_data,
    print_funnel_report,
    funnel_to_markdown,
    funnel_to_json,
)
from src.signals.types import BuySignalResult, SellSignalResult

import src.data.db as db_mod


# ── DB fixture (same isolation pattern as Phase 14) ───────────────────────────

@pytest.fixture()
def _db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "_sqlite_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "_get_supabase", lambda: None)
    db_mod.init_db()
    return db_file


# ═══════════════════════════════════════════════════════════════════════════════
# TestAssetUniverse
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssetUniverse:
    def test_btc_in_core_crypto(self):
        entry = get_entry("BTC/USD")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.CORE_CRYPTO
        assert entry.asset_class == "crypto"
        assert entry.is_meme is False

    def test_nvda_in_momentum_stocks(self):
        entry = get_entry("NVDA")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.CORE_MOMENTUM_STOCKS
        assert entry.asset_class == "stock"

    def test_spy_in_index_momentum(self):
        entry = get_entry("SPY")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.CORE_INDEX_MOMENTUM
        assert entry.asset_class == "etf"

    def test_tqqq_in_high_beta(self):
        entry = get_entry("TQQQ")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.HIGH_BETA_ETFS
        assert entry.asset_class == "etf"

    def test_doge_in_core_crypto(self):
        """DOGE is now Core Tier 1 crypto (liquid/momentum), not meme lane."""
        entry = get_entry("DOGE/USD")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.CORE_CRYPTO
        assert entry.is_meme is False
        assert is_meme("DOGE/USD") is False

    def test_shib_is_meme(self):
        """SHIB remains in the meme lane."""
        entry = get_entry("SHIB/USD")
        assert entry is not None
        assert entry.universe_group == UniverseGroup.MEME_COIN_LANE
        assert entry.is_meme is True
        assert is_meme("SHIB/USD") is True

    def test_unknown_symbol(self):
        assert get_entry("ZZZZZZ") is None
        assert is_known("ZZZZZZ") is False
        assert is_meme("ZZZZZZ") is False
        assert universe_group("ZZZZZZ") is None
        assert asset_class("ZZZZZZ") is None

    def test_is_known(self):
        assert is_known("BTC/USD") is True
        assert is_known("NVDA") is True
        assert is_known("PEPE/USD") is True

    def test_all_groups(self):
        groups = all_groups()
        assert len(groups) == 5
        assert UniverseGroup.CORE_CRYPTO in groups
        assert UniverseGroup.MEME_COIN_LANE in groups

    def test_get_symbols_for_group(self):
        crypto = get_symbols_for_group(UniverseGroup.CORE_CRYPTO)
        assert "BTC/USD" in crypto
        assert "ETH/USD" in crypto
        assert "AAPL" not in crypto

    def test_registry_snapshot_is_copy(self):
        snap = registry_snapshot()
        assert isinstance(snap, dict)
        assert "BTC/USD" in snap
        # Mutating the copy shouldn't affect the module
        snap.pop("BTC/USD")
        assert get_entry("BTC/USD") is not None

    def test_filter_to_universe_passes_unknown(self):
        """Unknown symbols are let through (operator override)."""
        result = filter_to_universe(["BTC/USD", "CUSTOM_SYM_123"])
        assert "CUSTOM_SYM_123" in result

    def test_filter_to_universe_blocks_disabled_group(self):
        """When a group is disabled, its symbols are filtered out."""
        import src.scanner.asset_universe as au_mod
        original = au_mod._GROUP_ENABLED.copy()
        try:
            au_mod._GROUP_ENABLED[UniverseGroup.MEME_COIN_LANE] = False
            # DOGE/USD is now CORE_CRYPTO (enabled); SHIB/USD is MEME_COIN_LANE (disabled)
            result = filter_to_universe(["BTC/USD", "DOGE/USD", "SHIB/USD"])
            assert "BTC/USD" in result
            assert "DOGE/USD" in result    # CORE_CRYPTO — still enabled
            assert "SHIB/USD" not in result  # MEME_COIN_LANE — disabled
        finally:
            au_mod._GROUP_ENABLED.update(original)

    def test_get_enabled_symbols_default(self):
        """All groups enabled by default — all symbols returned."""
        enabled = get_enabled_symbols()
        assert len(enabled) > 0
        assert "BTC/USD" in enabled
        assert "DOGE/USD" in enabled


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterVolatility
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterVolatility:
    def test_scalp_passes_above_threshold(self):
        ok, reason = check_volatility(2.0, "SCALP")
        assert ok is True
        assert reason == ""

    def test_scalp_fails_below_threshold(self):
        ok, reason = check_volatility(1.0, "SCALP")
        assert ok is False
        assert reason == SKIP_LOW_VOLATILITY

    def test_intermediate_threshold(self):
        ok, _ = check_volatility(1.8, "INTERMEDIATE")
        assert ok is False
        ok, _ = check_volatility(2.5, "INTERMEDIATE")
        assert ok is True

    def test_swing_threshold(self):
        ok, _ = check_volatility(2.0, "SWING")
        assert ok is False
        ok, _ = check_volatility(3.0, "SWING")
        assert ok is True

    def test_unknown_mode_uses_scalp(self):
        ok, _ = check_volatility(1.5, "UNKNOWN")
        assert ok is True

    def test_exact_boundary_passes(self):
        """ATR% exactly at the threshold should pass."""
        ok, _ = check_volatility(1.5, "SCALP")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterVolume
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterVolume:
    def test_normal_volume_passes(self):
        ok, reason = check_volume(1.6, 2.0, "SCALP")
        assert ok is True

    def test_normal_volume_fails(self):
        ok, reason = check_volume(0.8, 2.0, "SCALP")
        assert ok is False
        assert reason == SKIP_WEAK_VOLUME

    def test_strong_atr_uses_weak_threshold(self):
        """When ATR is very strong (>=1.5x threshold), weaker volume bar applies."""
        # SCALP threshold = 1.5%, so strong = 1.5*1.5 = 2.25%
        # With ATR 3.0% (strong), weak threshold of 1.0x should apply
        ok, _ = check_volume(1.1, 3.0, "SCALP")
        assert ok is True

    def test_weak_volume_with_borderline_atr(self):
        # ATR 2.0 with SCALP (threshold=1.5, strong=2.25) → not strong, needs 1.5x
        ok, _ = check_volume(1.2, 2.0, "SCALP")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterMemeLane
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterMemeLane:
    def test_meme_passes_all_checks(self):
        ok, reason = check_meme_lane(4.0, 2.5, 200000)
        assert ok is True

    def test_meme_low_volatility(self):
        ok, reason = check_meme_lane(2.0, 2.5, 200000)
        assert ok is False
        assert reason == SKIP_MEME_LOW_VOLATILITY

    def test_meme_weak_volume(self):
        ok, reason = check_meme_lane(4.0, 1.2, 200000)
        assert ok is False
        assert reason == SKIP_MEME_WEAK_VOLUME

    def test_meme_low_liquidity(self):
        ok, reason = check_meme_lane(4.0, 2.5, 50000)
        assert ok is False
        assert reason == SKIP_MEME_LOW_LIQUIDITY


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterPipeline
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterPipeline:
    def test_run_prefilter_passes_core_crypto(self):
        result = run_prefilter("BTC/USD", 3.0, 2.0, 500000, "SCALP")
        assert result.passed is True
        assert result.universe_group == "CORE_CRYPTO"
        assert result.is_meme is False
        assert result.rank_score > 0

    def test_run_prefilter_blocks_low_atr(self):
        result = run_prefilter("BTC/USD", 0.5, 2.0, 500000, "SCALP")
        assert result.passed is False
        assert result.skip_reason == SKIP_LOW_VOLATILITY

    def test_run_prefilter_blocks_weak_volume(self):
        result = run_prefilter("BTC/USD", 2.0, 0.5, 500000, "SCALP")
        assert result.passed is False
        assert result.skip_reason == SKIP_WEAK_VOLUME

    def test_run_prefilter_meme_extra_gate(self):
        """Meme coin passes general gates but fails meme-specific check."""
        result = run_prefilter("SHIB/USD", 2.0, 1.8, 500000, "SCALP")
        # ATR 2.0 passes SCALP threshold (1.5) but fails meme threshold (3.0)
        assert result.passed is False
        assert result.skip_reason == SKIP_MEME_LOW_VOLATILITY

    def test_run_prefilter_meme_full_pass(self):
        result = run_prefilter("SHIB/USD", 4.0, 2.5, 200000, "SCALP")
        assert result.passed is True
        assert result.is_meme is True

    def test_run_prefilter_unknown_symbol(self):
        """Unknown symbols have no universe group or meme flag."""
        result = run_prefilter("CUSTOM123", 3.0, 2.0, 500000, "SCALP")
        assert result.passed is True
        assert result.universe_group is None
        assert result.is_meme is False

    def test_select_top_candidates_caps(self):
        results = [
            PrefilterResult(symbol=f"SYM{i}", passed=True, rank_score=float(i))
            for i in range(20)
        ]
        top = select_top_candidates(results, top_n=5)
        assert len(top) == 5
        # Should be the top-5 by rank_score
        assert top[0].symbol == "SYM19"
        assert top[4].symbol == "SYM15"

    def test_select_top_candidates_marks_cutoff(self):
        results = [
            PrefilterResult(symbol=f"SYM{i}", passed=True, rank_score=float(i))
            for i in range(10)
        ]
        top = select_top_candidates(results, top_n=3)
        assert len(top) == 3
        # Check that the ones below the cutoff are marked
        cut = [r for r in results if r.skip_reason == SKIP_BELOW_RANK_CUTOFF]
        assert len(cut) == 7

    def test_select_top_candidates_excludes_failed(self):
        results = [
            PrefilterResult(symbol="A", passed=True, rank_score=5.0),
            PrefilterResult(symbol="B", passed=False, rank_score=10.0, skip_reason=SKIP_LOW_VOLATILITY),
            PrefilterResult(symbol="C", passed=True, rank_score=3.0),
        ]
        top = select_top_candidates(results, top_n=10)
        symbols = [r.symbol for r in top]
        assert "A" in symbols
        assert "C" in symbols
        assert "B" not in symbols

    def test_compute_rank_score(self):
        score = compute_rank_score(atr_pct=3.0, volume_ratio=2.0, alligator_spread=1.0)
        expected = 3.0 * 0.40 + min(2.0, 3.0) * 0.30 + 1.0 * 0.30
        assert abs(score - expected) < 1e-6

    def test_compute_rank_score_caps_volume(self):
        """Volume ratio capped at 3.0 in ranking."""
        s1 = compute_rank_score(3.0, 5.0, 1.0)
        s2 = compute_rank_score(3.0, 3.0, 1.0)
        assert abs(s1 - s2) < 1e-6


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterQualityFirst
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterQualityFirst:
    def test_quality_first_raises_threshold(self, monkeypatch):
        """Quality-first mode multiplies all ATR thresholds by 1.5."""
        import src.scanner.prefilters as pf_mod
        monkeypatch.setattr(pf_mod, "PREFILTER_QUALITY_FIRST", True)
        # SCALP threshold 1.5 * 1.5 = 2.25; ATR 2.0 should fail
        result = run_prefilter("BTC/USD", 2.0, 2.0, 500000, "SCALP")
        assert result.passed is False
        assert result.skip_reason == SKIP_LOW_VOLATILITY

    def test_quality_first_passes_high_atr(self, monkeypatch):
        import src.scanner.prefilters as pf_mod
        monkeypatch.setattr(pf_mod, "PREFILTER_QUALITY_FIRST", True)
        # SCALP threshold 1.5 * 1.5 = 2.25; ATR 3.0 should pass
        result = run_prefilter("BTC/USD", 3.0, 2.0, 500000, "SCALP")
        assert result.passed is True

    def test_quality_first_off_by_default(self):
        from src.config import PREFILTER_QUALITY_FIRST
        assert PREFILTER_QUALITY_FIRST is False


# ═══════════════════════════════════════════════════════════════════════════════
# TestFunnelReporter
# ═══════════════════════════════════════════════════════════════════════════════

class TestFunnelReporter:
    def _make_results(self):
        return [
            PrefilterResult("BTC/USD",  passed=True,  atr_pct=3.0, volume_ratio=2.0, rank_score=2.0, universe_group="CORE_CRYPTO"),
            PrefilterResult("ETH/USD",  passed=True,  atr_pct=2.5, volume_ratio=1.8, rank_score=1.5, universe_group="CORE_CRYPTO"),
            PrefilterResult("AAPL",     passed=False, atr_pct=0.5, volume_ratio=1.0, skip_reason=SKIP_LOW_VOLATILITY, universe_group="CORE_MOMENTUM_STOCKS"),
            PrefilterResult("DOGE/USD", passed=False, atr_pct=2.0, volume_ratio=1.5, skip_reason=SKIP_MEME_LOW_VOLATILITY, universe_group="MEME_COIN_LANE", is_meme=True),
            PrefilterResult("SPY",      passed=False, atr_pct=1.0, volume_ratio=0.5, skip_reason=SKIP_WEAK_VOLUME, universe_group="CORE_INDEX_MOMENTUM"),
        ]

    def test_build_funnel_data_structure(self):
        results = self._make_results()
        data = build_funnel_data(results, total_symbols=50, timeframe="1h", mode="INTERMEDIATE")
        assert data["total_symbols"] == 50
        assert data["timeframe"] == "1h"
        assert data["mode"] == "INTERMEDIATE"
        assert data["survivor_count"] == 2
        assert data["volatility_report"]["blocked"] == 2  # AAPL + DOGE
        assert data["volume_report"]["blocked"] == 1  # SPY
        assert data["meme_lane_report"]["total_meme"] == 1
        assert data["meme_lane_report"]["blocked"] == 1

    def test_funnel_to_markdown(self):
        results = self._make_results()
        data = build_funnel_data(results, total_symbols=50)
        md = funnel_to_markdown(data)
        assert "# Prefilter Funnel" in md
        assert "Survivors" in md
        assert "BTC/USD" in md

    def test_funnel_to_json_valid(self):
        results = self._make_results()
        data = build_funnel_data(results, total_symbols=50)
        j = funnel_to_json(data)
        parsed = json.loads(j)
        assert parsed["survivor_count"] == 2

    def test_print_funnel_report(self, capsys):
        results = self._make_results()
        data = build_funnel_data(results, total_symbols=50, timeframe="5m", mode="SCALP")
        print_funnel_report(data)
        captured = capsys.readouterr()
        assert "Prefilter Funnel" in captured.out
        assert "Survivors" in captured.out

    def test_empty_results(self):
        data = build_funnel_data([], total_symbols=0)
        assert data["survivor_count"] == 0
        assert data["volatility_report"]["blocked"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# TestSignalPrefilterFields
# ═══════════════════════════════════════════════════════════════════════════════

class TestSignalPrefilterFields:
    def test_buy_signal_has_prefilter_fields(self):
        sig = BuySignalResult()
        assert sig.prefilter_universe_group is None
        assert sig.prefilter_atr_pct is None
        assert sig.prefilter_volume_ratio is None
        assert sig.prefilter_rank_score is None
        assert sig.prefilter_passed is None
        assert sig.prefilter_skip_reason == ""

    def test_sell_signal_has_prefilter_fields(self):
        sig = SellSignalResult()
        assert sig.prefilter_universe_group is None
        assert sig.prefilter_atr_pct is None
        assert sig.prefilter_volume_ratio is None
        assert sig.prefilter_rank_score is None
        assert sig.prefilter_passed is None
        assert sig.prefilter_skip_reason == ""

    def test_buy_signal_prefilter_assignment(self):
        sig = BuySignalResult()
        sig.prefilter_universe_group = "CORE_CRYPTO"
        sig.prefilter_atr_pct = 3.5
        sig.prefilter_volume_ratio = 2.1
        sig.prefilter_rank_score = 1.8
        sig.prefilter_passed = True
        sig.prefilter_skip_reason = ""
        assert sig.prefilter_universe_group == "CORE_CRYPTO"
        assert sig.prefilter_atr_pct == 3.5


# ═══════════════════════════════════════════════════════════════════════════════
# TestDBMigration
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBMigration:
    def test_prefilter_columns_exist(self, _db):
        """After init_db, prefilter columns should be present on both signal tables."""
        conn = sqlite3.connect(str(_db))
        conn.row_factory = sqlite3.Row
        for table in ("buy_signals", "sell_signals"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for expected in (
                "prefilter_universe_group",
                "prefilter_atr_pct",
                "prefilter_volume_ratio",
                "prefilter_rank_score",
                "prefilter_passed",
                "prefilter_skip_reason",
            ):
                assert expected in cols, f"{expected} not in {table}"
        conn.close()

    def test_signal_row_includes_prefilter(self, _db):
        """_signal_row should include the prefilter fields."""
        sig = BuySignalResult(
            asset="BTC/USD",
            timeframe="1h",
            is_valid=True,
            entry_price=50000.0,
            stop_loss=49000.0,
        )
        sig.prefilter_universe_group = "CORE_CRYPTO"
        sig.prefilter_atr_pct = 3.5
        sig.prefilter_volume_ratio = 2.0
        sig.prefilter_rank_score = 1.8
        sig.prefilter_passed = True
        sig.prefilter_skip_reason = ""

        row = db_mod._signal_row(sig)
        assert row["prefilter_universe_group"] == "CORE_CRYPTO"
        assert row["prefilter_atr_pct"] == 3.5
        assert row["prefilter_volume_ratio"] == 2.0
        assert row["prefilter_rank_score"] == 1.8
        assert row["prefilter_passed"] == 1  # bool → int
        assert row["prefilter_skip_reason"] == ""

    def test_save_signal_with_prefilter_roundtrip(self, _db):
        """save_signal should persist prefilter columns."""
        sig = BuySignalResult(
            asset="ETH/USD",
            timeframe="5m",
            is_valid=True,
            entry_price=3000.0,
            stop_loss=2900.0,
        )
        sig.prefilter_universe_group = "CORE_CRYPTO"
        sig.prefilter_atr_pct = 2.8
        sig.prefilter_passed = True

        db_mod.save_signal(sig)

        conn = sqlite3.connect(str(_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM buy_signals WHERE asset='ETH/USD'").fetchone()
        assert row is not None
        assert row["prefilter_universe_group"] == "CORE_CRYPTO"
        assert abs(row["prefilter_atr_pct"] - 2.8) < 0.01
        assert row["prefilter_passed"] == 1
        conn.close()

    def test_migration_idempotent(self, _db):
        """Calling migrate_add_prefilter_columns twice shouldn't error."""
        db_mod.migrate_add_prefilter_columns()
        db_mod.migrate_add_prefilter_columns()


# ═══════════════════════════════════════════════════════════════════════════════
# TestConfigKnobs
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigKnobs:
    def test_universe_flags_exist(self):
        from src.config import (
            UNIVERSE_CORE_CRYPTO_ENABLED,
            UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED,
            UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED,
            UNIVERSE_HIGH_BETA_ETFS_ENABLED,
            UNIVERSE_MEME_COIN_LANE_ENABLED,
        )
        assert isinstance(UNIVERSE_CORE_CRYPTO_ENABLED, bool)
        assert isinstance(UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED, bool)
        assert isinstance(UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED, bool)
        assert isinstance(UNIVERSE_HIGH_BETA_ETFS_ENABLED, bool)
        assert isinstance(UNIVERSE_MEME_COIN_LANE_ENABLED, bool)

    def test_atr_thresholds(self):
        from src.config import (
            PREFILTER_ATR_MIN_SCALP,
            PREFILTER_ATR_MIN_INTERMEDIATE,
            PREFILTER_ATR_MIN_SWING,
        )
        assert PREFILTER_ATR_MIN_SCALP == 1.5
        assert PREFILTER_ATR_MIN_INTERMEDIATE == 2.0
        assert PREFILTER_ATR_MIN_SWING == 2.5

    def test_volume_thresholds(self):
        from src.config import (
            PREFILTER_VOLUME_EXPANSION_NORMAL,
            PREFILTER_VOLUME_EXPANSION_WEAK,
        )
        assert PREFILTER_VOLUME_EXPANSION_NORMAL == 1.5
        assert PREFILTER_VOLUME_EXPANSION_WEAK == 1.0

    def test_meme_thresholds(self):
        from src.config import (
            PREFILTER_MEME_ATR_MIN,
            PREFILTER_MEME_VOLUME_MIN,
            PREFILTER_MEME_AVG_VOLUME_FLOOR,
        )
        assert PREFILTER_MEME_ATR_MIN == 3.0
        assert PREFILTER_MEME_VOLUME_MIN == 2.0
        assert PREFILTER_MEME_AVG_VOLUME_FLOOR == 100000

    def test_top_n_and_quality_first(self):
        from src.config import PREFILTER_TOP_N, PREFILTER_QUALITY_FIRST
        assert PREFILTER_TOP_N == 10
        assert PREFILTER_QUALITY_FIRST is False

    def test_all_universe_defaults_true(self):
        """All universe groups default to enabled."""
        from src.config import (
            UNIVERSE_CORE_CRYPTO_ENABLED,
            UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED,
            UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED,
            UNIVERSE_HIGH_BETA_ETFS_ENABLED,
            UNIVERSE_MEME_COIN_LANE_ENABLED,
        )
        assert UNIVERSE_CORE_CRYPTO_ENABLED is True
        assert UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED is True
        assert UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED is True
        assert UNIVERSE_HIGH_BETA_ETFS_ENABLED is True
        assert UNIVERSE_MEME_COIN_LANE_ENABLED is True


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrefilterResultContract
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrefilterResultContract:
    """Verify PrefilterResult dataclass contract."""

    def test_defaults(self):
        r = PrefilterResult(symbol="X")
        assert r.passed is True
        assert r.skip_reason == ""
        assert r.atr_pct == 0.0
        assert r.volume_ratio == 0.0
        assert r.avg_volume == 0.0
        assert r.rank_score == 0.0
        assert r.universe_group is None
        assert r.is_meme is False

    def test_skip_reason_constants(self):
        """All skip reason constants are non-empty strings."""
        for code in (
            SKIP_LOW_VOLATILITY,
            SKIP_WEAK_VOLUME,
            SKIP_MEME_LOW_VOLATILITY,
            SKIP_MEME_WEAK_VOLUME,
            SKIP_MEME_LOW_LIQUIDITY,
            SKIP_BELOW_RANK_CUTOFF,
        ):
            assert isinstance(code, str)
            assert len(code) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TestUniverseGroupEnum
# ═══════════════════════════════════════════════════════════════════════════════

class TestUniverseGroupEnum:
    def test_values(self):
        assert UniverseGroup.CORE_CRYPTO.value == "CORE_CRYPTO"
        assert UniverseGroup.MEME_COIN_LANE.value == "MEME_COIN_LANE"

    def test_string_behaviour(self):
        """UniverseGroup is a str enum — can be used as dict key or compared to strings."""
        assert UniverseGroup.CORE_CRYPTO == "CORE_CRYPTO"

    def test_all_members(self):
        members = list(UniverseGroup)
        assert len(members) == 5
