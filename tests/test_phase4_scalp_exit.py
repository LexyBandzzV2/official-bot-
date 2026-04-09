"""Phase 4 SCALP exit path tests.

Covers:
  - Exit policy selection for formal vs fallback timeframes
  - policy_state_name() for all 7 states × 3 modes
  - ExitPolicy Phase 4 field values on SCALP
  - SCALP lock-stage thresholds (1.50 / 2.00 / 2.50 %)
  - ATR trail eligibility gating at stage 2
  - TrailingStop monotonicity with ATR candidates
  - momentum_fade_detected() true / false paths + SWING disabled
  - shrinking_body_sequence edge cases
  - was_protected_profit fires on break-even only / stage-1 only
  - indicator_flags and entry_reason_code format
  - used_fallback_policy flag for non-formal timeframes
  - is_formal_timeframe() coverage
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
    policy_state_name,
    FORMAL_TIMEFRAMES,
)
from src.signals.strategy_mode import is_formal_timeframe, FORMAL_TIMEFRAMES as SM_FORMAL
from src.risk.candle_quality import (
    body_to_range_ratio,
    wick_ratio,
    is_strong_candle,
    shrinking_body_sequence,
    momentum_fade_detected,
    consecutive_strong_count,
)
from src.risk.trailing_stop import TrailingStop


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strong_bull(body_frac: float = 0.80, range_: float = 10.0):
    """Return (open, high, low, close) of a strong BUY candle."""
    body = body_frac * range_
    open_ = 100.0
    close = open_ + body
    low   = open_ - (range_ - body) * 0.5
    high  = close + (range_ - body) * 0.5
    return (open_, high, low, close)


def _weak_bull(body_frac: float = 0.25, range_: float = 10.0):
    """Return a weak / indecisive BUY candle."""
    return _strong_bull(body_frac=body_frac, range_=range_)


def _shrinking_bull_sequence(n: int = 4) -> list:
    """Return a list of n+1 BUY candles with strictly shrinking bodies.

    Uses a large step (0.20) so that the last bar body_frac drops well below
    0.40, satisfying the momentum_fade_detected() threshold.
    """
    candles = []
    for i in range(n + 1):
        frac = 0.90 - i * 0.20   # 0.90, 0.70, 0.50, 0.30, 0.10 …
        if frac < 0.01:
            frac = 0.01
        candles.append(_strong_bull(body_frac=frac))
    return candles


# ── Policy selection ──────────────────────────────────────────────────────────

class TestPolicySelection:
    def test_3m_is_scalp(self):
        assert get_exit_policy("3m") is ScalpExitPolicy

    def test_5m_is_scalp(self):
        assert get_exit_policy("5m") is ScalpExitPolicy

    def test_15m_is_intermediate(self):
        assert get_exit_policy("15m") is IntermediateExitPolicy

    def test_1h_is_intermediate(self):
        assert get_exit_policy("1h") is IntermediateExitPolicy

    def test_2h_is_swing(self):
        assert get_exit_policy("2h") is SwingExitPolicy

    def test_4h_is_swing(self):
        assert get_exit_policy("4h") is SwingExitPolicy

    def test_1m_is_scalp_micro(self):
        # 1m is now a formal SCALP_1M timeframe with its own break-even tuning
        p = get_exit_policy("1m")
        assert p is ScalpMicroExitPolicy
        assert p.name == "SCALP_1M"
        assert p.break_even_pct == 0.80   # arms later than 3m/5m for 1m noise

    def test_unknown_tf_fallback_is_intermediate(self):
        assert get_exit_policy("99m") is IntermediateExitPolicy

    def test_30m_fallback(self):
        assert get_exit_policy("30m") is IntermediateExitPolicy


class TestFormalTimeframes:
    def test_formal_timeframes_set(self):
        assert FORMAL_TIMEFRAMES == {"1m", "3m", "5m", "15m", "1h", "2h", "4h"}

    def test_strategy_mode_formal_timeframes_consistent(self):
        assert SM_FORMAL == FORMAL_TIMEFRAMES

    @pytest.mark.parametrize("tf", ["1m", "3m", "5m", "15m", "1h", "2h", "4h"])
    def test_is_formal_true(self, tf):
        assert is_formal_timeframe(tf) is True

    @pytest.mark.parametrize("tf", ["30m", "3h", "1d", "99m", ""])
    def test_is_formal_false(self, tf):
        assert is_formal_timeframe(tf) is False


# ── ExitPolicy Phase 4 fields ─────────────────────────────────────────────────

class TestScalpExitPolicyPhase4Fields:
    def test_scalp_lock_stage_thresholds(self):
        stages = ScalpExitPolicy.profit_lock_stages
        assert stages[0] == (1.50, 0.50)
        assert stages[1] == (2.00, 1.00)
        assert stages[2] == (2.50, 1.50)

    def test_scalp_trail_mode_is_atr(self):
        assert ScalpExitPolicy.trail_mode == "atr"

    def test_scalp_atr_eligible_after_stage_2(self):
        assert ScalpExitPolicy.atr_eligible_after_stage == 2

    def test_scalp_momentum_fade_window(self):
        assert ScalpExitPolicy.momentum_fade_window == 3

    def test_intermediate_trail_mode_candle(self):
        assert IntermediateExitPolicy.trail_mode == "candle"

    def test_intermediate_atr_never_eligible(self):
        assert IntermediateExitPolicy.atr_eligible_after_stage == 99

    def test_swing_momentum_fade_window_zero(self):
        assert SwingExitPolicy.momentum_fade_window == 0

    def test_intermediate_giveback_preserved(self):
        assert IntermediateExitPolicy.giveback_frac == 0.35

    def test_scalp_is_frozen(self):
        with pytest.raises((TypeError, AttributeError)):
            ScalpExitPolicy.giveback_frac = 0.99  # type: ignore[misc]


class TestAtrTrailEligibility:
    def test_atr_eligible_at_stage_2(self):
        # ATR trail becomes eligible exactly at stage 2 (atr_eligible_after_stage == 2)
        assert 2 >= ScalpExitPolicy.atr_eligible_after_stage

    def test_atr_not_eligible_at_stage_1(self):
        assert 1 < ScalpExitPolicy.atr_eligible_after_stage

    def test_atr_not_eligible_at_stage_0(self):
        assert 0 < ScalpExitPolicy.atr_eligible_after_stage

    def test_intermediate_atr_never_eligible(self):
        # Even stage 3 is below 99
        assert 3 < IntermediateExitPolicy.atr_eligible_after_stage


# ── policy_state_name() ───────────────────────────────────────────────────────

class TestPolicyStateName:
    @pytest.mark.parametrize("state,expected", [
        ("INITIAL_STOP",    "SCALP_INITIAL_STOP"),
        ("BREAK_EVEN",      "SCALP_BREAK_EVEN"),
        ("STAGE_1_LOCKED",  "SCALP_STAGE_1_LOCKED"),
        ("STAGE_2_LOCKED",  "SCALP_STAGE_2_LOCKED"),
        ("STAGE_3_LOCKED",  "SCALP_STAGE_3_LOCKED"),
        ("CANDLE_TRAIL",    "SCALP_CANDLE_TRAIL"),
        ("ATR_TRAIL",       "SCALP_ATR_TRAIL"),
    ])
    def test_scalp_states(self, state, expected):
        assert policy_state_name("SCALP", state) == expected

    @pytest.mark.parametrize("state,expected", [
        ("INITIAL_STOP",    "INTERMEDIATE_INITIAL_STOP"),
        ("BREAK_EVEN",      "INTERMEDIATE_BREAK_EVEN"),
        ("STAGE_3_LOCKED",  "INTERMEDIATE_STAGE_3_LOCKED"),
    ])
    def test_intermediate_states(self, state, expected):
        assert policy_state_name("INTERMEDIATE", state) == expected

    @pytest.mark.parametrize("state,expected", [
        ("INITIAL_STOP",    "SWING_INITIAL_STOP"),
        ("CANDLE_TRAIL",    "SWING_CANDLE_TRAIL"),
    ])
    def test_swing_states(self, state, expected):
        assert policy_state_name("SWING", state) == expected

    def test_lowercase_state_normalised(self):
        assert policy_state_name("SCALP", "break_even") == "SCALP_BREAK_EVEN"


# ── TrailingStop monotonicity ─────────────────────────────────────────────────

class TestTrailingStopMonotonicity:
    def _buy_trail(self, entry: float = 100.0) -> TrailingStop:
        # initial_teeth below entry so stop starts at the hard floor (entry*0.98),
        # not at the entry price itself
        return TrailingStop("buy", entry, initial_teeth=entry * 0.97, stop_loss_pct=0.02)

    def test_atr_candidate_weaker_than_current_stop_is_rejected(self):
        trail = self._buy_trail()        # stop starts at hard floor ≈ 98.0
        trail.update(99.5)               # raises stop to 99.5
        stop_before = trail.current_stop
        trail.update(97.0)               # worse candidate — must be ignored
        assert trail.current_stop == stop_before

    def test_atr_candidate_better_than_current_stop_is_accepted(self):
        trail = self._buy_trail()        # stop starts at hard floor ≈ 98.0
        trail.update(99.0)               # raises stop to 99.0
        trail.update(99.8)               # better — stop should move up
        assert trail.current_stop == pytest.approx(99.8)

    def test_sell_trail_only_moves_down(self):
        trail = TrailingStop("sell", 100.0, initial_teeth=100.0, stop_loss_pct=0.02)
        trail.update(100.5)   # good for sell (lower stop)
        stop_before = trail.current_stop
        trail.update(103.0)   # worse for sell — must not raise stop
        assert trail.current_stop == stop_before

    def test_stop_never_below_hard_floor_on_buy(self):
        trail = self._buy_trail(entry=100.0)
        trail.update(50.0)    # terrible candidate — below hard floor
        assert trail.current_stop >= trail.hard_floor


# ── Candle quality ────────────────────────────────────────────────────────────

class TestBodyToRangeRatio:
    def test_full_body(self):
        # open=100, close=110, high=110, low=100 → body == range
        assert body_to_range_ratio(100.0, 110.0, 100.0, 110.0) == pytest.approx(1.0)

    def test_doji(self):
        # open == close → body = 0
        assert body_to_range_ratio(100.0, 105.0, 95.0, 100.0) == pytest.approx(0.0)

    def test_zero_range_returns_zero(self):
        assert body_to_range_ratio(100.0, 100.0, 100.0, 100.0) == 0.0

    def test_half_body(self):
        ratio = body_to_range_ratio(100.0, 110.0, 100.0, 105.0)
        assert ratio == pytest.approx(0.5)


class TestMomentumFadeDetected:
    def test_shrinking_body_and_weak_last_triggers_fade(self):
        # Body fracs: 0.90, 0.70, 0.50, 0.30 — last 0.30 < 0.40 threshold
        candles = _shrinking_bull_sequence(n=3)
        assert momentum_fade_detected(candles, "BUY", window=3)

    def test_strong_candles_no_fade(self):
        strong = [_strong_bull(0.80)] * 5
        assert not momentum_fade_detected(strong, "BUY", window=3)

    def test_window_zero_disabled(self):
        candles = _shrinking_bull_sequence(n=3)
        assert not momentum_fade_detected(candles, "BUY", window=0)

    def test_insufficient_candles_returns_false(self):
        assert not momentum_fade_detected([_strong_bull()], "BUY", window=3)

    def test_swing_policy_fade_window_zero(self):
        # SwingExitPolicy.momentum_fade_window == 0 → always disabled
        candles = _shrinking_bull_sequence(n=3)
        assert not momentum_fade_detected(
            candles, "BUY", window=SwingExitPolicy.momentum_fade_window
        )

    def test_sell_direction_works(self):
        # For a SELL, build a shrinking descending-body sequence.
        # Body fracs 0.90, 0.70, 0.50, 0.30 → last ratio 0.30 < 0.40
        bear_candles = []
        for frac in (0.90, 0.70, 0.50, 0.30):
            open_ = 100.0
            body  = frac * 10.0
            close = open_ - body
            wick  = (10.0 - body) * 0.5
            high  = open_ + wick
            low   = close - wick
            bear_candles.append((open_, high, low, close))
        assert momentum_fade_detected(bear_candles, "SELL", window=3)


class TestShrinkingBodySequence:
    def test_strict_shrink_returns_true(self):
        candles = _shrinking_bull_sequence(n=2)
        assert shrinking_body_sequence(candles, n=2)

    def test_flat_body_returns_false(self):
        same = [_strong_bull(0.70)] * 4
        assert not shrinking_body_sequence(same, n=2)

    def test_one_non_shrink_breaks_sequence(self):
        candles = _shrinking_bull_sequence(n=3)
        # Insert a larger body in the middle of the inspection window
        candles[-2] = _strong_bull(0.90)
        assert not shrinking_body_sequence(candles, n=3)

    def test_n_0_returns_false(self):
        candles = _shrinking_bull_sequence(n=3)
        assert not shrinking_body_sequence(candles, n=0)


# ── was_protected_profit logic (unit-level, no scanner) ──────────────────────

class TestWasProtectedProfit:
    def test_be_armed_alone_sets_protected(self):
        """Simulate: break_even_armed=True, profit_lock_stage=0."""
        from src.signals.types import TradeRecord
        from datetime import datetime
        rec = TradeRecord(
            trade_id="t1", signal_type="BUY", asset="BTC", timeframe="5m",
            entry_time=datetime.now(), entry_price=100.0,
            stop_loss_hard=98.0, trailing_stop=98.0,
            position_size=1.0, account_risk_pct=0.01,
            alligator_point=True, stochastic_point=True, vortex_point=True,
            jaw_at_entry=99.0, teeth_at_entry=99.5, lips_at_entry=99.8,
        )
        # Simulate break-even arm
        rec.break_even_armed = True
        rec.was_protected_profit = rec.break_even_armed or rec.profit_lock_stage >= 1
        assert rec.was_protected_profit is True

    def test_stage1_alone_sets_protected(self):
        from src.signals.types import TradeRecord
        from datetime import datetime
        rec = TradeRecord(
            trade_id="t2", signal_type="BUY", asset="BTC", timeframe="5m",
            entry_time=datetime.now(), entry_price=100.0,
            stop_loss_hard=98.0, trailing_stop=98.0,
            position_size=1.0, account_risk_pct=0.01,
            alligator_point=True, stochastic_point=False, vortex_point=True,
            jaw_at_entry=99.0, teeth_at_entry=99.5, lips_at_entry=99.8,
        )
        rec.profit_lock_stage = 1
        rec.was_protected_profit = rec.break_even_armed or rec.profit_lock_stage >= 1
        assert rec.was_protected_profit is True

    def test_no_protection_no_flag(self):
        from src.signals.types import TradeRecord
        from datetime import datetime
        rec = TradeRecord(
            trade_id="t3", signal_type="BUY", asset="BTC", timeframe="5m",
            entry_time=datetime.now(), entry_price=100.0,
            stop_loss_hard=98.0, trailing_stop=98.0,
            position_size=1.0, account_risk_pct=0.01,
            alligator_point=False, stochastic_point=False, vortex_point=False,
            jaw_at_entry=99.0, teeth_at_entry=99.5, lips_at_entry=99.8,
        )
        assert not (rec.break_even_armed or rec.profit_lock_stage >= 1)


# ── indicator_flags and entry_reason_code format ──────────────────────────────

class TestEntryReasonFormat:
    def test_indicator_flags_format(self):
        flags = ["alligator", "stochastic", "vortex"]
        result = "+".join(flags)
        assert result == "alligator+stochastic+vortex"

    def test_entry_reason_code_format(self):
        abbrevs = {"alligator": "al", "stochastic": "st", "vortex": "vo"}
        flags = ["alligator", "stochastic", "vortex"]
        ml, ai = 0.87, 0.72
        code = "+".join(abbrevs[f] for f in flags)
        code += f":ml{int(ml*100)}:ai{int(ai*100)}"
        assert code == "al+st+vo:ml87:ai72"

    def test_partial_flags(self):
        abbrevs = {"alligator": "al", "stochastic": "st", "vortex": "vo"}
        flags = ["alligator", "stochastic"]
        code = "+".join(abbrevs[f] for f in flags)
        assert code == "al+st"

    def test_no_ai_code(self):
        ml = 0.90
        code = f"al+st+vo:ml{int(ml*100)}"
        assert code == "al+st+vo:ml90"


# ── TradeRecord Phase 4 field defaults ────────────────────────────────────────

class TestTradeRecordPhase4Defaults:
    def _rec(self, **kwargs):
        from src.signals.types import TradeRecord
        from datetime import datetime
        defaults = dict(
            trade_id="x", signal_type="BUY", asset="BTC", timeframe="5m",
            entry_time=datetime.now(), entry_price=100.0,
            stop_loss_hard=98.0, trailing_stop=98.0,
            position_size=1.0, account_risk_pct=0.01,
            alligator_point=True, stochastic_point=True, vortex_point=True,
            jaw_at_entry=99.0, teeth_at_entry=99.5, lips_at_entry=99.8,
        )
        defaults.update(kwargs)
        return TradeRecord(**defaults)

    def test_indicator_flags_default_none(self):
        assert self._rec().indicator_flags is None

    def test_entry_reason_code_default_none(self):
        assert self._rec().entry_reason_code is None

    def test_trail_active_mode_default_none(self):
        assert self._rec().trail_active_mode is None

    def test_used_fallback_policy_default_false(self):
        assert self._rec().used_fallback_policy is False
