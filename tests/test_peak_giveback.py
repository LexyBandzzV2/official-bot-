"""Unit tests for PeakGiveback and its integration with RiskManager.

Coverage:
  - Long and short trigger paths
  - No-trigger before a meaningful favorable move exists (peak == entry guard)
  - Exact trigger-level boundary (on the line fires, above the line for long does not)
  - Losing-close scenario: trigger fires even though close < entry
  - trigger_level() arithmetic for both directions
  - Peak ratchet behaviour (only moves in favorable direction)
  - RiskManager.check_exit_conditions priority:
      HARD_STOP → PEAK_GIVEBACK_EXIT → TRAIL_STOP
  - Emitted label is 'PEAK_GIVEBACK_EXIT', never the legacy 'TRAILING_TP'
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from src.risk.trailing_take_profit import PeakGiveback
from src.risk.risk_manager import RiskManager
from src.risk.trailing_stop import TrailingStop
from src.signals.types import TradeRecord


# ── module-level helpers ───────────────────────────────────────────────────

def _pg(direction: str, entry: float, giveback: float = 0.35) -> PeakGiveback:
    return PeakGiveback(direction=direction, entry_price=entry, giveback_frac=giveback)


def _make_open_trade(
    signal_type: str = "BUY",
    entry: float = 100.0,
    stop_hard: float = 98.0,
    teeth: float = 99.0,
) -> tuple[RiskManager, str, TradeRecord]:
    """Build a RiskManager with one registered open position."""
    rm = RiskManager(
        account_balance=10_000.0,
        stop_loss_pct=0.02,
        max_risk_per_trade=0.01,
    )
    tid = str(uuid.uuid4())
    rec = TradeRecord(
        trade_id         = tid,
        signal_type      = signal_type,
        asset            = "BTCUSDT",
        timeframe        = "15m",
        entry_time       = datetime.now(),
        entry_price      = entry,
        stop_loss_hard   = stop_hard,
        trailing_stop    = stop_hard,
        position_size    = 1.0,
        account_risk_pct = 1.0,
        alligator_point  = True,
        stochastic_point = True,
        vortex_point     = True,
        jaw_at_entry     = 97.0,
        teeth_at_entry   = teeth,
        lips_at_entry    = 100.5,
    )
    trail = TrailingStop(
        direction     = "buy" if signal_type == "BUY" else "sell",
        entry_price   = entry,
        initial_teeth = teeth,
        stop_loss_pct = 0.02,
    )
    rec._trail_stop = trail  # type: ignore[attr-defined]
    rm.open_positions[tid] = rec
    return rm, tid, rec


# ── PeakGiveback unit tests ────────────────────────────────────────────────

class TestPeakGivebackLong:
    """BUY direction."""

    def test_no_trigger_before_favorable_move(self):
        """Peak == entry means no MFE yet; trigger must not fire at any price."""
        pg = _pg("buy", 100.0)
        pg.update_bar(high=100.0, low=99.0)   # peak stays at 100
        assert pg.is_triggered(99.5) is False
        assert pg.is_triggered(100.5) is False

    def test_no_trigger_before_retrace(self):
        """Enough favorable move but price hasn't retraced yet."""
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)  # peak=110, trigger=106.5
        assert pg.is_triggered(107.0) is False   # 107.0 > 106.5 — safe

    def test_trigger_fires_on_exact_level(self):
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)
        assert pg.trigger_level() == pytest.approx(106.5)
        assert pg.is_triggered(106.5) is True    # on the line fires

    def test_trigger_fires_below_level(self):
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)
        assert pg.is_triggered(105.0) is True

    def test_no_trigger_above_level(self):
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)
        assert pg.is_triggered(106.51) is False

    def test_trigger_level_math(self):
        """trigger_level = peak - frac * (peak - entry)."""
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)
        assert pg.trigger_level() == pytest.approx(110.0 - 0.35 * 10.0)

    def test_peak_ratchets_only_upward(self):
        pg = _pg("buy", 100.0)
        pg.update_bar(high=110.0, low=100.0)
        pg.update_bar(high=105.0, low=98.0)   # lower high — peak must not drop
        assert pg.peak_price == pytest.approx(110.0)

    def test_loss_close_trigger_below_entry(self):
        """Extreme giveback_frac places trigger below entry; exit fires at a loss."""
        pg = _pg("buy", 100.0, giveback=2.0)  # trigger = 100.5 - 2.0*0.5 = 99.5
        pg.update_bar(high=100.5, low=100.0)
        assert pg.trigger_level() == pytest.approx(99.5)
        assert pg.is_triggered(99.4) is True   # below entry — loss is allowed


class TestPeakGivebackShort:
    """SELL direction (favorable move is downward)."""

    def test_no_trigger_before_favorable_move(self):
        pg = _pg("sell", 100.0)
        pg.update_bar(high=101.0, low=100.0)  # peak stays at entry
        assert pg.is_triggered(99.0) is False
        assert pg.is_triggered(100.5) is False

    def test_trigger_fires_on_exact_level(self):
        """peak=90, entry=100, frac=0.35 → trigger=93.5; close==93.5 fires."""
        pg = _pg("sell", 100.0, 0.35)
        pg.update_bar(high=100.0, low=90.0)
        assert pg.trigger_level() == pytest.approx(93.5)
        assert pg.is_triggered(93.5) is True

    def test_trigger_fires_above_level_for_short(self):
        pg = _pg("sell", 100.0, 0.35)
        pg.update_bar(high=100.0, low=90.0)  # trigger=93.5
        assert pg.is_triggered(94.0) is True   # price above trigger fires for short

    def test_no_trigger_below_level_for_short(self):
        pg = _pg("sell", 100.0, 0.35)
        pg.update_bar(high=100.0, low=90.0)
        assert pg.is_triggered(93.4) is False

    def test_trigger_level_math(self):
        """trigger_level = peak + frac * (entry - peak)."""
        pg = _pg("sell", 100.0, 0.35)
        pg.update_bar(high=100.0, low=90.0)
        assert pg.trigger_level() == pytest.approx(90.0 + 0.35 * 10.0)

    def test_peak_ratchets_only_downward(self):
        pg = _pg("sell", 100.0)
        pg.update_bar(high=100.0, low=90.0)
        pg.update_bar(high=100.0, low=95.0)  # higher low — peak must not rise
        assert pg.peak_price == pytest.approx(90.0)

    def test_loss_close_trigger_above_entry(self):
        """Extreme giveback_frac places trigger above entry; still fires."""
        pg = _pg("sell", 100.0, giveback=2.0)
        pg.update_bar(high=100.0, low=99.5)   # peak=99.5, trigger=99.5+2.0*0.5=100.5
        assert pg.trigger_level() == pytest.approx(100.5)
        assert pg.is_triggered(100.6) is True  # above entry — loss allowed


class TestPeakGivebackInvalidDirection:
    def test_bad_direction_raises(self):
        with pytest.raises(ValueError):
            PeakGiveback(direction="long", entry_price=100.0, giveback_frac=0.35)


# ── RiskManager integration tests ─────────────────────────────────────────

class TestRiskManagerExitPriority:
    """Verify priority chain: HARD_STOP → PEAK_GIVEBACK_EXIT → TRAIL_STOP."""

    def test_hard_stop_beats_peak_giveback(self):
        rm, tid, _ = _make_open_trade(entry=100.0, stop_hard=98.0)
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=90.0)
        # price 97.9 hits HARD_STOP; peak_giveback would also fire — HARD_STOP wins
        fired, reason = rm.check_exit_conditions(tid, 97.9, peak_giveback=pg)
        assert fired is True
        assert reason == "HARD_STOP"

    def test_peak_giveback_exit_label(self):
        """check_exit_conditions must emit 'PEAK_GIVEBACK_EXIT', not 'TRAILING_TP'."""
        rm, tid, _ = _make_open_trade(entry=100.0, stop_hard=97.0)
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)  # peak=110, trigger_level=106.5
        # price 106.0 is below trigger (106.5) and above hard stop (97)
        fired, reason = rm.check_exit_conditions(tid, 106.0, peak_giveback=pg)
        assert fired is True
        assert reason == "PEAK_GIVEBACK_EXIT", (
            f"Expected 'PEAK_GIVEBACK_EXIT', got '{reason}'. "
            "The legacy TRAILING_TP label must no longer be emitted."
        )

    def test_no_exit_price_above_trigger(self):
        rm, tid, _ = _make_open_trade(entry=100.0, stop_hard=97.0)
        pg = _pg("buy", 100.0, 0.35)
        pg.update_bar(high=110.0, low=100.0)  # trigger=106.5
        fired, _ = rm.check_exit_conditions(tid, 108.0, peak_giveback=pg)
        assert fired is False

    def test_no_peak_giveback_never_emits_reason(self):
        """When peak_giveback=None the exit reason must never be PEAK_GIVEBACK_EXIT."""
        rm, tid, _ = _make_open_trade(entry=100.0, stop_hard=97.0)
        fired, reason = rm.check_exit_conditions(tid, 99.0, peak_giveback=None)
        assert reason != "PEAK_GIVEBACK_EXIT"

    def test_trail_stop_fires_when_no_peak_giveback(self):
        """TRAIL_STOP is next in the chain when pg is None."""
        rm, tid, rec = _make_open_trade(entry=100.0, stop_hard=97.0, teeth=99.0)
        trail: TrailingStop = rec._trail_stop  # type: ignore[attr-defined]
        trail.update(103.0)   # drive stop up to 103
        # price 100.5 < trail (103) and > hard stop (97) → TRAIL_STOP
        fired, reason = rm.check_exit_conditions(tid, 100.5, peak_giveback=None)
        assert fired is True
        assert reason == "TRAIL_STOP"

    def test_losing_close_via_peak_giveback_is_valid(self):
        """PEAK_GIVEBACK_EXIT may close below entry — that must not be prevented."""
        rm, tid, _ = _make_open_trade(entry=100.0, stop_hard=97.0)
        pg = _pg("buy", 100.0, giveback=2.0)   # trigger = 100.5 - 2.0*0.5 = 99.5
        pg.update_bar(high=100.5, low=100.0)
        # price 99.4 < entry (losing close) and > hard stop (97) → PEAK_GIVEBACK_EXIT
        fired, reason = rm.check_exit_conditions(tid, 99.4, peak_giveback=pg)
        assert fired is True
        assert reason == "PEAK_GIVEBACK_EXIT"

    def test_unknown_trade_id_returns_no_exit(self):
        rm = RiskManager()
        fired, reason = rm.check_exit_conditions("nonexistent-id", 100.0)
        assert fired is False
        assert reason == ""
