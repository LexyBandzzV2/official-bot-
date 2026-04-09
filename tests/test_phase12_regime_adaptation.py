"""Phase 12 tests — Regime-Aware Strategy Adaptation.

Coverage:
  TestMacroRegimeMapping      — MacroRegime enum, RegimeLabel.macro_labels()
  TestRegimeContextExtended   — new Phase 12 fields, to_dict, to_log_str, macro_labels
  TestRegimeScoreBias         — additive score adjustment per macro regime
  TestRegimeEntryFilter       — accept/reject per macro regime + min score
  TestAdaptExitParams         — giveback, break-even, fade multipliers per macro
  TestBuildContextWithHistory — previous_label, duration, asset, timeframe
  TestTradeRecordPhase12      — new fields default correctly, backward compat
  TestRegimeReporterPhase12   — avg_mae, transition_stability in report data
  TestRegimeAdapterFailOpen   — None ctx, low confidence, UNCERTAIN: no-op
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pytest

from src.signals.regime_types import (
    RegimeLabel,
    RegimeContext,
    RegimeSnapshot,
    MacroRegime,
    VolatilityMetrics,
    TrendMetrics,
    ChopMetrics,
)
from src.signals.regime_adapter import (
    apply_regime_score_bias,
    check_regime_entry_filter,
    adapt_exit_params,
)
from src.signals.regime_gating import (
    build_regime_context_for_signal,
    populate_regime_modifiers,
)
from src.signals.types import BuySignalResult, SellSignalResult, TradeRecord
from src.tools.regime_reporter import (
    get_regime_report_data,
    regime_to_markdown,
    regime_to_json,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_snapshot(
    label: RegimeLabel = RegimeLabel.TRENDING_HIGH_VOL,
    confidence: float = 0.75,
    asset: str = "BTC/USD",
    timeframe: str = "1h",
    created_at: datetime | None = None,
) -> RegimeSnapshot:
    import uuid as _uuid
    return RegimeSnapshot(
        regime_id=str(_uuid.uuid4()),
        created_at=created_at or datetime.now(timezone.utc),
        asset=asset,
        asset_class="crypto",
        timeframe=timeframe,
        strategy_mode="SCALP",
        regime_label=label,
        confidence_score=confidence,
        evidence_summary="test snapshot",
    )


def _make_ctx(
    label: RegimeLabel = RegimeLabel.TRENDING_HIGH_VOL,
    confidence: float = 0.75,
) -> RegimeContext:
    return RegimeContext(
        regime_label=label,
        confidence_score=confidence,
    )


def _make_sig(score: float = 50.0) -> BuySignalResult:
    sig = BuySignalResult(
        asset="BTC/USD",
        timeframe="1h",
        signal_type="BUY",
        is_valid=True,
        score_total=score,
    )
    return sig


# ══════════════════════════════════════════════════════════════════════════════
# TestMacroRegimeMapping
# ══════════════════════════════════════════════════════════════════════════════


class TestMacroRegimeMapping:
    """MacroRegime enum values and RegimeLabel.macro_labels() mapping."""

    def test_macro_regime_values(self):
        assert set(MacroRegime) == {
            MacroRegime.TRENDING,
            MacroRegime.RANGING,
            MacroRegime.HIGH_VOL,
            MacroRegime.LOW_VOL,
            MacroRegime.UNCERTAIN,
        }

    def test_trending_high_vol_maps_to_trending_and_high_vol(self):
        macros = RegimeLabel.TRENDING_HIGH_VOL.macro_labels()
        assert MacroRegime.TRENDING in macros
        assert MacroRegime.HIGH_VOL in macros
        assert len(macros) == 2

    def test_trending_low_vol_maps_to_trending_and_low_vol(self):
        macros = RegimeLabel.TRENDING_LOW_VOL.macro_labels()
        assert MacroRegime.TRENDING in macros
        assert MacroRegime.LOW_VOL in macros

    def test_choppy_high_vol_maps_to_ranging_and_high_vol(self):
        macros = RegimeLabel.CHOPPY_HIGH_VOL.macro_labels()
        assert MacroRegime.RANGING in macros
        assert MacroRegime.HIGH_VOL in macros

    def test_choppy_low_vol_maps_to_ranging_and_low_vol(self):
        macros = RegimeLabel.CHOPPY_LOW_VOL.macro_labels()
        assert MacroRegime.RANGING in macros
        assert MacroRegime.LOW_VOL in macros

    def test_reversal_transition_maps_to_uncertain(self):
        macros = RegimeLabel.REVERSAL_TRANSITION.macro_labels()
        assert MacroRegime.UNCERTAIN in macros

    def test_news_driven_maps_to_uncertain_and_high_vol(self):
        macros = RegimeLabel.NEWS_DRIVEN_UNSTABLE.macro_labels()
        assert MacroRegime.UNCERTAIN in macros
        assert MacroRegime.HIGH_VOL in macros

    def test_unknown_maps_to_uncertain(self):
        macros = RegimeLabel.UNKNOWN.macro_labels()
        assert MacroRegime.UNCERTAIN in macros


# ══════════════════════════════════════════════════════════════════════════════
# TestRegimeContextExtended
# ══════════════════════════════════════════════════════════════════════════════


class TestRegimeContextExtended:
    """Phase 12 fields on RegimeContext."""

    def test_default_new_fields(self):
        ctx = RegimeContext()
        assert ctx.previous_label is None
        assert ctx.regime_duration_seconds == 0.0
        assert ctx.timestamp is None
        assert ctx.asset == ""
        assert ctx.timeframe == ""
        assert ctx.regime_score_adjustment == 0.0
        assert ctx.regime_score_reason == ""
        assert ctx.regime_entry_allowed is True
        assert ctx.regime_entry_reason == ""

    def test_to_dict_includes_new_fields(self):
        ctx = _make_ctx()
        ctx.previous_label = RegimeLabel.CHOPPY_LOW_VOL
        ctx.regime_duration_seconds = 300.0
        ctx.asset = "ETH/USD"
        ctx.timeframe = "5m"
        d = ctx.to_dict()
        assert d["previous_label"] == "CHOPPY_LOW_VOL"
        assert d["regime_duration_seconds"] == 300.0
        assert d["asset"] == "ETH/USD"
        assert d["timeframe"] == "5m"
        assert "regime_score_adjustment" in d
        assert "regime_entry_allowed" in d

    def test_to_log_str_includes_prev_and_duration(self):
        ctx = _make_ctx()
        ctx.previous_label = RegimeLabel.CHOPPY_HIGH_VOL
        ctx.regime_duration_seconds = 120.0
        s = ctx.to_log_str()
        assert "prev=CHOPPY_HIGH_VOL" in s
        assert "dur=120s" in s

    def test_macro_labels_on_context(self):
        ctx = _make_ctx(label=RegimeLabel.CHOPPY_LOW_VOL)
        macros = ctx.macro_labels()
        assert MacroRegime.RANGING in macros
        assert MacroRegime.LOW_VOL in macros

    def test_macro_labels_on_none_label(self):
        ctx = RegimeContext()
        macros = ctx.macro_labels()
        assert MacroRegime.UNCERTAIN in macros


# ══════════════════════════════════════════════════════════════════════════════
# TestRegimeScoreBias
# ══════════════════════════════════════════════════════════════════════════════


class TestRegimeScoreBias:
    """apply_regime_score_bias() additive score modifications."""

    def test_trending_positive_bias(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.TRENDING_HIGH_VOL, 0.80)
        apply_regime_score_bias(sig, ctx)
        # TRENDING gives +3, HIGH_VOL gives -3, net = 0
        assert sig.score_total == 50.0
        # But let's test pure trending
        sig2 = _make_sig(50.0)
        ctx2 = _make_ctx(RegimeLabel.TRENDING_LOW_VOL, 0.80)
        apply_regime_score_bias(sig2, ctx2)
        # TRENDING +3, LOW_VOL -2, net = +1
        assert sig2.score_total == 51.0

    def test_ranging_negative_bias(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_LOW_VOL, 0.70)
        apply_regime_score_bias(sig, ctx)
        # RANGING -5, LOW_VOL -2, net = -7
        assert sig.score_total == 43.0

    def test_no_bias_for_none_ctx(self):
        sig = _make_sig(50.0)
        apply_regime_score_bias(sig, None)
        assert sig.score_total == 50.0

    def test_no_bias_for_low_confidence(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.20)
        apply_regime_score_bias(sig, ctx)
        assert sig.score_total == 50.0

    def test_no_bias_for_unknown_label(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.UNKNOWN, 0.80)
        apply_regime_score_bias(sig, ctx)
        assert sig.score_total == 50.0

    def test_observability_fields_populated(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_LOW_VOL, 0.70)
        apply_regime_score_bias(sig, ctx)
        assert ctx.regime_score_adjustment != 0.0
        assert ctx.regime_score_reason != ""

    def test_uncertain_zero_bias(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.REVERSAL_TRANSITION, 0.60)
        apply_regime_score_bias(sig, ctx)
        # UNCERTAIN has 0 bias by default
        assert sig.score_total == 50.0


# ══════════════════════════════════════════════════════════════════════════════
# TestRegimeEntryFilter
# ══════════════════════════════════════════════════════════════════════════════


class TestRegimeEntryFilter:
    """check_regime_entry_filter() accept/reject rules."""

    def test_trending_allows_lower_score(self):
        sig = _make_sig(35.0)
        ctx = _make_ctx(RegimeLabel.TRENDING_LOW_VOL, 0.80)
        # TRENDING min=30, LOW_VOL min=40 → strictest = 40
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert not allowed
        assert "regime_entry_rejected" in reason

    def test_trending_allows_adequate_score(self):
        sig = _make_sig(45.0)
        ctx = _make_ctx(RegimeLabel.TRENDING_LOW_VOL, 0.80)
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert allowed

    def test_ranging_requires_higher_score(self):
        sig = _make_sig(45.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.70)
        # RANGING min=50, HIGH_VOL min=45 → strictest = 50
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert not allowed

    def test_none_ctx_allows_entry(self):
        sig = _make_sig(10.0)
        allowed, reason = check_regime_entry_filter(sig, None)
        assert allowed

    def test_low_confidence_allows_entry(self):
        sig = _make_sig(10.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.20)
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert allowed

    def test_sets_ctx_fields_on_reject(self):
        sig = _make_sig(30.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.70)
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert not allowed
        assert ctx.regime_entry_allowed is False
        assert ctx.regime_entry_reason != ""

    def test_sets_ctx_fields_on_accept(self):
        sig = _make_sig(60.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.70)
        allowed, reason = check_regime_entry_filter(sig, ctx)
        assert allowed
        assert ctx.regime_entry_allowed is True


# ══════════════════════════════════════════════════════════════════════════════
# TestAdaptExitParams
# ══════════════════════════════════════════════════════════════════════════════


class TestAdaptExitParams:
    """adapt_exit_params() multiplier application."""

    def test_trending_widens_giveback(self):
        ctx = _make_ctx(RegimeLabel.TRENDING_LOW_VOL, 0.80)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        # TRENDING gb mult = 1.20, LOW_VOL gb mult = 1.10 → 1.32
        assert gb > 0.35
        assert "TRENDING" in reason

    def test_ranging_tightens_giveback(self):
        ctx = _make_ctx(RegimeLabel.CHOPPY_LOW_VOL, 0.70)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        # RANGING gb mult = 0.80, LOW_VOL mult = 1.10 → 0.88
        assert gb < 0.35

    def test_high_vol_arms_break_even_earlier(self):
        ctx = _make_ctx(RegimeLabel.TRENDING_HIGH_VOL, 0.80)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        # TRENDING be mult = 1.10, HIGH_VOL be mult = 0.75 → 0.825 → be = 0.60 * 0.825 = 0.495
        assert be < 0.60

    def test_none_ctx_returns_originals(self):
        gb, be, fade, reason = adapt_exit_params(None, 0.35, 0.60, 0.30)
        assert gb == 0.35
        assert be == 0.60
        assert fade == 0.30
        assert reason == ""

    def test_low_confidence_returns_originals(self):
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.10)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        assert gb == 0.35

    def test_safety_clamps_applied(self):
        # Even with extreme multipliers, values stay in bounds
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.90)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.05, 0.05, 0.05)
        assert gb >= 0.10  # min clamp
        assert be >= 0.10
        assert fade >= 0.10


# ══════════════════════════════════════════════════════════════════════════════
# TestBuildContextWithHistory
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildContextWithHistory:
    """build_regime_context_for_signal() with previous snapshot."""

    def test_basic_context_built(self):
        snap = _make_snapshot()
        ctx = build_regime_context_for_signal(snap)
        assert ctx.regime_label == RegimeLabel.TRENDING_HIGH_VOL
        assert ctx.asset == "BTC/USD"
        assert ctx.timeframe == "1h"

    def test_previous_snapshot_sets_fields(self):
        t1 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
        prev = _make_snapshot(RegimeLabel.CHOPPY_LOW_VOL, created_at=t1)
        curr = _make_snapshot(RegimeLabel.TRENDING_HIGH_VOL, created_at=t2)
        ctx = build_regime_context_for_signal(curr, previous_snapshot=prev)
        assert ctx.previous_label == RegimeLabel.CHOPPY_LOW_VOL
        assert ctx.regime_duration_seconds == 300.0

    def test_no_previous_snapshot(self):
        snap = _make_snapshot()
        ctx = build_regime_context_for_signal(snap, previous_snapshot=None)
        assert ctx.previous_label is None
        assert ctx.regime_duration_seconds == 0.0

    def test_modifier_fields_populated(self):
        snap = _make_snapshot(RegimeLabel.CHOPPY_LOW_VOL, confidence=0.80)
        ctx = build_regime_context_for_signal(snap)
        # Should have non-zero ml_threshold_delta for CHOPPY_LOW_VOL
        assert ctx.ml_threshold_delta != 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TestTradeRecordPhase12
# ══════════════════════════════════════════════════════════════════════════════


class TestTradeRecordPhase12:
    """New Phase 12 fields on TradeRecord default correctly."""

    def test_defaults(self):
        rec = TradeRecord(
            trade_id="test-1",
            signal_type="BUY",
            asset="BTC/USD",
            timeframe="1h",
            entry_time=datetime.now(timezone.utc),
            entry_price=50000.0,
            stop_loss_hard=49000.0,
            trailing_stop=49500.0,
            position_size=0.01,
            account_risk_pct=0.01,
            alligator_point=True,
            stochastic_point=True,
            vortex_point=True,
            jaw_at_entry=49800.0,
            teeth_at_entry=49900.0,
            lips_at_entry=50000.0,
        )
        assert rec.regime_label_at_exit is None
        assert rec.regime_confidence_at_exit == 0.0
        assert rec.regime_changed_during_trade is False
        assert rec.regime_transition_count == 0
        assert rec.regime_score_adjustment == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TestRegimeReporterPhase12
# ══════════════════════════════════════════════════════════════════════════════


class TestRegimeReporterPhase12:
    """Phase 12 reporter additions."""

    def test_bucket_stats_include_avg_mae(self):
        from src.tools.regime_reporter import _compute_bucket_stats
        trades = [
            {"pnl_pct": 2.0, "max_unrealized_profit": 3.0, "min_unrealized_profit": -1.5, "score_total": 60, "regime_changed_during_trade": 0},
            {"pnl_pct": -1.0, "max_unrealized_profit": 0.5, "min_unrealized_profit": -2.0, "score_total": 40, "regime_changed_during_trade": 1},
        ]
        stats = _compute_bucket_stats(trades)
        assert "avg_mae" in stats
        assert stats["avg_mae"] == pytest.approx(-1.75, abs=0.01)
        assert "regime_change_rate" in stats
        assert stats["regime_change_rate"] == 0.5

    def test_transition_stability_in_report(self):
        from src.tools.regime_reporter import _transition_stability
        trades = [
            {"pnl_pct": 2.0, "regime_changed_during_trade": 0},
            {"pnl_pct": -1.0, "regime_changed_during_trade": 1},
            {"pnl_pct": 1.0, "regime_changed_during_trade": 0},
        ]
        ts = _transition_stability(trades)
        assert ts["total_trades"] == 3
        assert ts["trades_with_regime_change"] == 1
        assert ts["change_rate"] == pytest.approx(0.3333, abs=0.01)
        assert ts["avg_pnl_stable"] == pytest.approx(1.5, abs=0.01)
        assert ts["avg_pnl_changed"] == pytest.approx(-1.0, abs=0.01)

    def test_markdown_includes_mae_column(self):
        data = {
            "generated_at": "2025-01-01T00:00:00",
            "total_closed_trades": 1,
            "total_regime_snapshots": 1,
            "regime_stats": {
                "TRENDING_HIGH_VOL": {
                    "total_trades": 5,
                    "win_rate": 0.60,
                    "avg_pnl": 1.5,
                    "avg_mfe": 3.0,
                    "avg_mae": -1.2,
                    "avg_leakage": 1.5,
                    "capture_ratio": 0.50,
                    "avg_score_total": 65.0,
                    "regime_change_rate": 0.2,
                },
            },
            "mode_regime_stats": {},
            "snapshot_distribution": {},
            "threshold_diagnostics": {},
            "transition_stability": {
                "total_trades": 5,
                "trades_with_regime_change": 1,
                "change_rate": 0.20,
                "avg_pnl_stable": 2.0,
                "avg_pnl_changed": -0.5,
                "win_rate_stable": 0.75,
                "win_rate_changed": 0.0,
            },
            "conclusions": {},
        }
        md = regime_to_markdown(data)
        assert "Avg MAE%" in md
        assert "Transition Stability" in md
        assert "regime change" in md.lower()


# ══════════════════════════════════════════════════════════════════════════════
# TestRegimeAdapterFailOpen
# ══════════════════════════════════════════════════════════════════════════════


class TestRegimeAdapterFailOpen:
    """All adapter functions must be fail-open when context is absent or weak."""

    def test_score_bias_none_ctx(self):
        sig = _make_sig(50.0)
        apply_regime_score_bias(sig, None)
        assert sig.score_total == 50.0

    def test_entry_filter_none_ctx(self):
        sig = _make_sig(10.0)
        ok, _ = check_regime_entry_filter(sig, None)
        assert ok

    def test_exit_params_none_ctx(self):
        gb, be, fade, reason = adapt_exit_params(None, 0.35, 0.60, 0.30)
        assert gb == 0.35
        assert be == 0.60
        assert fade == 0.30

    def test_score_bias_unknown_regime(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.UNKNOWN, 0.80)
        apply_regime_score_bias(sig, ctx)
        assert sig.score_total == 50.0

    def test_entry_filter_unknown_regime(self):
        sig = _make_sig(10.0)
        ctx = _make_ctx(RegimeLabel.UNKNOWN, 0.80)
        ok, _ = check_regime_entry_filter(sig, ctx)
        assert ok

    def test_exit_params_unknown_regime(self):
        ctx = _make_ctx(RegimeLabel.UNKNOWN, 0.80)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        assert gb == 0.35

    def test_score_bias_low_confidence(self):
        sig = _make_sig(50.0)
        ctx = _make_ctx(RegimeLabel.TRENDING_HIGH_VOL, 0.10)
        apply_regime_score_bias(sig, ctx)
        assert sig.score_total == 50.0

    def test_entry_filter_low_confidence(self):
        sig = _make_sig(10.0)
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.10)
        ok, _ = check_regime_entry_filter(sig, ctx)
        assert ok

    def test_exit_params_low_confidence(self):
        ctx = _make_ctx(RegimeLabel.CHOPPY_HIGH_VOL, 0.10)
        gb, be, fade, reason = adapt_exit_params(ctx, 0.35, 0.60, 0.30)
        assert gb == 0.35
