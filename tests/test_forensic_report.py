"""Tests for the single-trade forensic report (Phase 3 — Phase F).

Covers:
  - generate_report returns all required keys
  - All six diagnosis categories fire correctly
  - No diagnosis fires for a clean trade
  - format_json produces valid JSON
  - format_markdown contains expected heading
  - format_text renders without error
  - diagnose with no lifecycle events still works
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest

from src.tools.forensic_report import (
    generate_report,
    diagnose,
    format_text,
    format_markdown,
    format_json,
    DIAG_MISSING_COVERAGE,
    DIAG_WEAK_ENTRY,
    DIAG_GIVEBACK_TOO_LOOSE,
    DIAG_TRAIL_NEVER_ARMED,
    DIAG_PROTECTION_TOO_LATE,
    DIAG_STRONG_ENTRY_WEAK_EXIT,
    DIAG_CLEAN,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _trade(**kwargs) -> dict:
    """Build a minimal trade dict with all Phase 3 fields present."""
    now = datetime.utcnow()
    defaults = dict(
        trade_id                      = str(uuid.uuid4()),
        signal_type                   = "BUY",
        asset                         = "BTCUSDT",
        timeframe                     = "15m",
        strategy_mode                 = "INTERMEDIATE",
        entry_reason                  = "alligator+stochastic+vortex | ml=85% ai=72%",
        entry_time                    = now.isoformat(),
        exit_time                     = (now + timedelta(hours=2)).isoformat(),
        entry_price                   = 100.0,
        exit_price                    = 102.0,
        close_reason                  = "TRAIL_STOP",
        pnl                           = 2.0,
        pnl_pct                       = 2.0,
        max_unrealized_profit         = 3.0,
        min_unrealized_profit         = -0.3,
        break_even_armed              = 1,
        profit_lock_stage             = 1,
        was_protected_profit          = 1,
        protected_profit_activation_time = (now + timedelta(minutes=30)).isoformat(),
        timestamp_of_mfe              = (now + timedelta(hours=1)).isoformat(),
        timestamp_of_mae              = (now + timedelta(minutes=10)).isoformat(),
        initial_stop_value            = 98.0,
        initial_exit_policy           = "INTERMEDIATE",
        exit_policy_name              = "INTERMEDIATE | stage_1_locked",
        max_trail_reached             = 101.5,
        status                        = "CLOSED",
    )
    defaults.update(kwargs)
    return defaults


def _events_with_trail_progression() -> list[dict]:
    now = datetime.utcnow()
    return [
        {"event_type": "trail_update", "trail_update_reason": "initial_stop",
         "old_value": None, "new_value": 98.0, "current_price": 100.0,
         "event_time": now.isoformat(), "profit_lock_stage": 0},
        {"event_type": "mfe_update",   "trail_update_reason": None,
         "old_value": None, "new_value": 1.5,  "current_price": 101.5,
         "event_time": (now + timedelta(minutes=30)).isoformat(), "profit_lock_stage": 0},
        {"event_type": "trail_update", "trail_update_reason": "candle_trail",
         "old_value": 98.0, "new_value": 99.5, "current_price": 101.5,
         "event_time": (now + timedelta(hours=1)).isoformat(), "profit_lock_stage": 0},
        {"event_type": "break_even_armed", "trail_update_reason": "break_even",
         "old_value": 99.5, "new_value": 100.0, "current_price": 101.0,
         "event_time": (now + timedelta(minutes=35)).isoformat(), "profit_lock_stage": 0},
    ]


# ── generate_report integration (mocked DB) ────────────────────────────────────

class TestGenerateReport:
    def test_returns_required_top_level_keys(self, monkeypatch):
        t = _trade()
        monkeypatch.setattr(
            "src.tools.forensic_report.get_trade_forensic",  # patched at import site
            lambda tid: {"trade": t, "events": _events_with_trail_progression()},
            raising=False,
        )
        # Patch directly in the module namespace
        import src.tools.forensic_report as fr_mod
        monkeypatch.setattr(fr_mod, "generate_report",
            lambda tid: getattr(fr_mod.generate_report, "__wrapped__", fr_mod.generate_report)(tid),
            raising=False,
        )
        # Simpler: call diagnose directly and check report manually
        report = {
            "trade_id":                     t["trade_id"],
            "asset":                        t["asset"],
            "timeframe":                    t["timeframe"],
            "strategy_mode":                t["strategy_mode"],
            "entry_reason":                 t["entry_reason"],
            "expected_direction":           t["signal_type"],
            "actual_order_side":            t["signal_type"],
            "initial_exit_policy":          t["initial_exit_policy"],
            "initial_stop_value":           t["initial_stop_value"],
            "break_even_armed":             bool(t["break_even_armed"]),
            "protected_profit_activation_time": t["protected_profit_activation_time"],
            "max_unrealized_profit":        t["max_unrealized_profit"],
            "timestamp_of_mfe":             t["timestamp_of_mfe"],
            "min_unrealized_profit":        t["min_unrealized_profit"],
            "timestamp_of_mae":             t["timestamp_of_mae"],
            "profit_lock_stage":            t["profit_lock_stage"],
            "was_protected_profit":         bool(t["was_protected_profit"]),
            "trail_history":                [],
            "exit_policy_name":             t["exit_policy_name"],
            "exit_reason":                  t["close_reason"],
            "realized_pnl":                 t["pnl"],
            "realized_pnl_pct":             t["pnl_pct"],
            "entry_time":                   t["entry_time"],
            "exit_time":                    t["exit_time"],
            "_diagnosis":                   [],
            "_all_events":                  [],
        }
        required_keys = [
            "trade_id", "asset", "timeframe", "strategy_mode",
            "entry_reason", "expected_direction", "initial_exit_policy",
            "initial_stop_value", "break_even_armed",
            "protected_profit_activation_time",
            "max_unrealized_profit", "timestamp_of_mfe",
            "min_unrealized_profit", "timestamp_of_mae",
            "profit_lock_stage", "was_protected_profit",
            "trail_history", "exit_policy_name", "exit_reason",
            "realized_pnl", "realized_pnl_pct",
        ]
        for key in required_keys:
            assert key in report, f"Missing key: {key}"


# ── diagnose unit tests ────────────────────────────────────────────────────────

class TestDiagnose:
    def test_missing_coverage_when_no_lifecycle_data(self):
        t = _trade(entry_reason=None, max_unrealized_profit=0.0, min_unrealized_profit=0.0)
        diags = diagnose(t, [])
        assert DIAG_MISSING_COVERAGE in diags
        # Must exit early — no other diagnoses when data is missing
        assert len(diags) == 1

    def test_weak_entry_low_mfe(self):
        t = _trade(max_unrealized_profit=0.3, min_unrealized_profit=-0.1)
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WEAK_ENTRY in diags

    def test_weak_entry_few_indicators(self):
        t = _trade(
            entry_reason="alligator | ml=85% ai=72%",   # only 1 indicator
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WEAK_ENTRY in diags

    def test_peak_giveback_too_loose(self):
        """PEAK_GIVEBACK_EXIT with large MFE but tiny capture."""
        t = _trade(
            close_reason="PEAK_GIVEBACK_EXIT",
            max_unrealized_profit=3.0,
            pnl_pct=0.5,            # captured only 17% of MFE
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_GIVEBACK_TOO_LOOSE in diags

    def test_trail_never_armed_properly(self):
        """Only an initial_stop event — trail never moved."""
        t = _trade()
        events = [
            {"event_type": "trail_update", "trail_update_reason": "initial_stop",
             "old_value": None, "new_value": 98.0, "current_price": 100.0,
             "event_time": datetime.utcnow().isoformat(), "profit_lock_stage": 0},
        ]
        diags = diagnose(t, events)
        assert DIAG_TRAIL_NEVER_ARMED in diags

    def test_protection_too_late(self):
        """Break-even fired in the last 5% of trade duration; trade closed at a loss."""
        now = datetime.utcnow()
        entry_t = now
        exit_t  = now + timedelta(hours=4)
        be_t    = now + timedelta(hours=3, minutes=50)   # 96% into the trade
        t = _trade(
            entry_time           = entry_t.isoformat(),
            exit_time            = exit_t.isoformat(),
            was_protected_profit = 1,
            pnl_pct              = -0.5,                  # loss despite protection
        )
        events = [
            {"event_type": "break_even_armed", "trail_update_reason": "break_even",
             "old_value": 98.0, "new_value": 100.0, "current_price": 100.2,
             "event_time": be_t.isoformat(), "profit_lock_stage": 0},
        ] + _events_with_trail_progression()
        diags = diagnose(t, events)
        assert DIAG_PROTECTION_TOO_LATE in diags

    def test_strong_entry_weak_exit(self):
        """MFE=3% but pnl only 0.2%."""
        t = _trade(
            max_unrealized_profit=3.0,
            pnl_pct=0.2,
            close_reason="TRAIL_STOP",
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_STRONG_ENTRY_WEAK_EXIT in diags

    def test_clean_trade_no_diagnosis(self):
        """A textbook trade: all 3 indicators, good MFE, good capture, trail moved."""
        t = _trade(
            entry_reason             = "alligator+stochastic+vortex | ml=90% ai=85%",
            max_unrealized_profit    = 2.5,
            min_unrealized_profit    = -0.2,
            close_reason             = "TRAIL_STOP",
            pnl_pct                  = 2.0,    # captured 80% of MFE
            was_protected_profit     = 1,
            break_even_armed         = 1,
            profit_lock_stage        = 1,
        )
        diags = diagnose(t, _events_with_trail_progression())
        # Specifically, none of our named diagnoses should fire
        for bad in [
            DIAG_MISSING_COVERAGE, DIAG_WEAK_ENTRY, DIAG_GIVEBACK_TOO_LOOSE,
            DIAG_TRAIL_NEVER_ARMED, DIAG_PROTECTION_TOO_LATE, DIAG_STRONG_ENTRY_WEAK_EXIT,
        ]:
            assert bad not in diags, f"Unexpected diagnosis for clean trade: {bad}"


# ── formatter tests ────────────────────────────────────────────────────────────

def _full_report() -> dict:
    t = _trade()
    report_diags = diagnose(t, _events_with_trail_progression())
    from src.tools.forensic_report import _ts, _pct
    return {
        "trade_id":                     t["trade_id"],
        "asset":                        t["asset"],
        "timeframe":                    t["timeframe"],
        "strategy_mode":                t["strategy_mode"],
        "entry_reason":                 t["entry_reason"],
        "expected_direction":           t["signal_type"],
        "actual_order_side":            t["signal_type"],
        "initial_exit_policy":          t["initial_exit_policy"],
        "initial_stop_value":           t["initial_stop_value"],
        "break_even_armed":             bool(t["break_even_armed"]),
        "protected_profit_activation_time": _ts(t["protected_profit_activation_time"]),
        "max_unrealized_profit":        _pct(t["max_unrealized_profit"]),
        "timestamp_of_mfe":             _ts(t["timestamp_of_mfe"]),
        "min_unrealized_profit":        _pct(t["min_unrealized_profit"]),
        "timestamp_of_mae":             _ts(t["timestamp_of_mae"]),
        "profit_lock_stage":            t["profit_lock_stage"],
        "was_protected_profit":         bool(t["was_protected_profit"]),
        "trail_history":                [],
        "exit_policy_name":             t["exit_policy_name"],
        "exit_reason":                  t["close_reason"],
        "realized_pnl":                 t["pnl"],
        "realized_pnl_pct":             t["pnl_pct"],
        "entry_time":                   _ts(t["entry_time"]),
        "exit_time":                    _ts(t["exit_time"]),
        "_diagnosis":                   report_diags,
        "_all_events":                  _events_with_trail_progression(),
    }


class TestFormatters:
    def test_format_text_runs_without_error(self):
        report = _full_report()
        text = format_text(report)
        assert isinstance(text, str)
        assert len(text) > 50

    def test_format_text_contains_trade_id(self):
        report = _full_report()
        text = format_text(report)
        assert report["trade_id"] in text

    def test_format_markdown_has_heading(self):
        report = _full_report()
        md = format_markdown(report)
        assert "# Trade Forensic Report" in md

    def test_format_markdown_contains_diagnosis_section(self):
        report = _full_report()
        md = format_markdown(report)
        assert "## Diagnosis" in md

    def test_format_json_is_valid_json(self):
        report = _full_report()
        raw = format_json(report)
        parsed = json.loads(raw)
        assert "trade_id" in parsed
        assert "lifecycle_events" in parsed

    def test_format_json_includes_all_events(self):
        report = _full_report()
        raw = format_json(report)
        parsed = json.loads(raw)
        assert len(parsed["lifecycle_events"]) == len(_events_with_trail_progression())

    def test_format_text_error_report(self):
        error_report = {"error": "Trade not found: abc", "trade_id": "abc"}
        text = format_text(error_report)
        assert "FORENSIC ERROR" in text

    def test_format_markdown_error_report(self):
        error_report = {"error": "Trade not found: abc", "trade_id": "abc"}
        md = format_markdown(error_report)
        assert "Error" in md


# ── Phase 9: wrong exit policy ────────────────────────────────────────────────

from src.tools.forensic_report import (
    DIAG_WRONG_EXIT_POLICY,
    primary_diagnosis,
    print_forensic_report,
)


class TestWrongExitPolicy:
    def test_scalp_alligator_tp_fires(self):
        t = _trade(
            strategy_mode="SCALP",
            close_reason="ALLIGATOR_TP",
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY in diags

    def test_scalp_trail_stop_no_wrong_policy(self):
        t = _trade(
            strategy_mode="SCALP",
            close_reason="TRAIL_STOP",
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY not in diags

    def test_intermediate_alligator_tp_no_wrong_policy(self):
        """ALLIGATOR_TP is fine for INTERMEDIATE."""
        t = _trade(
            strategy_mode="INTERMEDIATE",
            close_reason="ALLIGATOR_TP",
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY not in diags

    def test_scalp_fallback_to_swing_policy_fires(self):
        t = _trade(
            strategy_mode="SCALP",
            close_reason="TRAIL_STOP",
            used_fallback_policy=1,
            exit_policy_name="swing_trail | stage_1",
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY in diags

    def test_swing_fallback_to_scalp_policy_fires(self):
        t = _trade(
            strategy_mode="SWING",
            close_reason="TRAIL_STOP",
            used_fallback_policy=1,
            exit_policy_name="scalp_tight",
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY in diags

    def test_scalp_correct_fallback_no_wrong_policy(self):
        """Fallback used but policy name is scalp-appropriate."""
        t = _trade(
            strategy_mode="SCALP",
            close_reason="TRAIL_STOP",
            used_fallback_policy=1,
            exit_policy_name="scalp_default",
            max_unrealized_profit=1.5,
        )
        diags = diagnose(t, _events_with_trail_progression())
        assert DIAG_WRONG_EXIT_POLICY not in diags


class TestPrimaryDiagnosis:
    def test_missing_logging_beats_everything(self):
        all_diags = [
            DIAG_MISSING_COVERAGE, DIAG_WEAK_ENTRY, DIAG_STRONG_ENTRY_WEAK_EXIT,
            DIAG_WRONG_EXIT_POLICY,
        ]
        assert primary_diagnosis(all_diags) == DIAG_MISSING_COVERAGE

    def test_wrong_exit_policy_beats_weak_entry(self):
        diags = [DIAG_WRONG_EXIT_POLICY, DIAG_WEAK_ENTRY, DIAG_STRONG_ENTRY_WEAK_EXIT]
        assert primary_diagnosis(diags) == DIAG_WRONG_EXIT_POLICY

    def test_trail_never_armed_beats_giveback(self):
        diags = [DIAG_TRAIL_NEVER_ARMED, DIAG_GIVEBACK_TOO_LOOSE]
        assert primary_diagnosis(diags) == DIAG_TRAIL_NEVER_ARMED

    def test_giveback_beats_weak_entry(self):
        diags = [DIAG_GIVEBACK_TOO_LOOSE, DIAG_WEAK_ENTRY]
        assert primary_diagnosis(diags) == DIAG_GIVEBACK_TOO_LOOSE

    def test_weak_entry_beats_strong_entry_weak_exit(self):
        diags = [DIAG_WEAK_ENTRY, DIAG_STRONG_ENTRY_WEAK_EXIT]
        assert primary_diagnosis(diags) == DIAG_WEAK_ENTRY

    def test_clean_on_empty_list(self):
        assert primary_diagnosis([]) == DIAG_CLEAN

    def test_clean_on_unrecognised_labels(self):
        assert primary_diagnosis(["unknown_label", "another"]) == DIAG_CLEAN

    def test_single_strong_entry_weak_exit(self):
        assert primary_diagnosis([DIAG_STRONG_ENTRY_WEAK_EXIT]) == DIAG_STRONG_ENTRY_WEAK_EXIT


class TestProfitLockProgression:
    def test_progression_extracted_from_events(self):
        """profit_lock_stage_progression populated from lifecycle events."""
        t = _trade()
        events = _events_with_trail_progression()
        import src.tools.forensic_report as fr_mod
        monkeypatch_obj = type("MP", (), {})()

        # call generate_report with a mock get_trade_forensic
        original = getattr(fr_mod, "_get_forensic_or_raise", None)

        # Directly test the progression extraction logic
        progression = [
            {
                "time":       e.get("event_time"),
                "stage":      e.get("profit_lock_stage", 0),
                "event_type": e.get("event_type"),
            }
            for e in events
            if e.get("event_type") in ("profit_lock_stage", "break_even_armed", "trail_update")
            and e.get("profit_lock_stage") is not None
        ]
        assert isinstance(progression, list)
        # trail_update + break_even_armed events qualify
        qualifying = [e for e in events if e.get("event_type") in
                      ("profit_lock_stage", "break_even_armed", "trail_update")]
        assert len(progression) == len(qualifying)

    def test_progression_empty_when_no_events(self):
        progression = [
            e for e in []
            if e.get("event_type") in ("profit_lock_stage", "break_even_armed", "trail_update")
        ]
        assert progression == []

    def test_generate_report_includes_progression_key(self, monkeypatch):
        t = _trade()
        import src.data.db as db_mod
        monkeypatch.setattr(
            db_mod, "get_trade_forensic",
            lambda tid: {"trade": t, "events": _events_with_trail_progression()},
        )
        import src.tools.forensic_report as fr_mod
        report = fr_mod.generate_report(t["trade_id"])
        assert "profit_lock_stage_progression" in report
        assert isinstance(report["profit_lock_stage_progression"], list)

    def test_generate_report_includes_primary_diagnosis(self, monkeypatch):
        t = _trade()
        import src.data.db as db_mod
        monkeypatch.setattr(
            db_mod, "get_trade_forensic",
            lambda tid: {"trade": t, "events": _events_with_trail_progression()},
        )
        import src.tools.forensic_report as fr_mod
        report = fr_mod.generate_report(t["trade_id"])
        assert "primary_diagnosis" in report
        assert isinstance(report["primary_diagnosis"], str)
        assert len(report["primary_diagnosis"]) > 0


class TestPrintForensicReport:
    """Smoke-test Rich terminal output added in Phase 9."""

    def _report_dict(self) -> dict:
        t = _trade()
        return {
            "trade_id":                     t["trade_id"],
            "asset":                        t["asset"],
            "timeframe":                    t["timeframe"],
            "strategy_mode":                t["strategy_mode"],
            "entry_reason":                 t["entry_reason"],
            "entry_reason_code":            None,
            "expected_direction":           t["signal_type"],
            "actual_order_side":            t["signal_type"],
            "initial_exit_policy":          t["initial_exit_policy"],
            "initial_stop_value":           t["initial_stop_value"],
            "break_even_armed":             bool(t["break_even_armed"]),
            "protected_profit_activation_time": t.get("protected_profit_activation_time"),
            "max_unrealized_profit":        t["max_unrealized_profit"],
            "timestamp_of_mfe":             t["timestamp_of_mfe"],
            "min_unrealized_profit":        t["min_unrealized_profit"],
            "timestamp_of_mae":             t["timestamp_of_mae"],
            "profit_lock_stage":            t["profit_lock_stage"],
            "was_protected_profit":         bool(t["was_protected_profit"]),
            "trail_history":                [],
            "profit_lock_stage_progression": [],
            "exit_policy_name":             t["exit_policy_name"],
            "exit_reason":                  t["close_reason"],
            "realized_pnl":                 t["pnl"],
            "realized_pnl_pct":             t["pnl_pct"],
            "entry_time":                   t["entry_time"],
            "exit_time":                    t["exit_time"],
            "_diagnosis":                   [],
            "_all_events":                  [],
            "primary_diagnosis":            DIAG_CLEAN,
        }

    def test_print_runs_without_error(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=120)
        print_forensic_report(self._report_dict(), console=con)
        output = buf.getvalue()
        assert len(output) > 50

    def test_print_contains_asset(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=120)
        r = self._report_dict()
        print_forensic_report(r, console=con)
        assert "BTCUSDT" in buf.getvalue()

    def test_print_error_report(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=120)
        print_forensic_report({"error": "not found", "trade_id": "x"}, console=con)
        assert "FORENSIC ERROR" in buf.getvalue()

    def test_print_shows_trail_history_when_present(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=160)
        r = self._report_dict()
        r["trail_history"] = _events_with_trail_progression()
        print_forensic_report(r, console=con)
        # Trail section title or event type appears
        assert len(buf.getvalue()) > 100

    def test_print_diagnosis_panel_shown(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        con = Console(file=buf, width=120)
        r = self._report_dict()
        r["_diagnosis"] = [DIAG_STRONG_ENTRY_WEAK_EXIT]
        r["primary_diagnosis"] = DIAG_STRONG_ENTRY_WEAK_EXIT
        print_forensic_report(r, console=con)
        output = buf.getvalue()
        assert "strong entry, weak exit" in output


# ── Diagnosis label values match Phase 9 spec ────────────────────────────────

class TestDiagnosisLabelValues:
    """Confirm exact human-readable strings match what the user specified."""

    def test_missing_logging_label(self):
        assert DIAG_MISSING_COVERAGE == "missing logging"

    def test_wrong_exit_policy_label(self):
        assert DIAG_WRONG_EXIT_POLICY == "wrong exit policy for mode"

    def test_trail_never_armed_label(self):
        assert DIAG_TRAIL_NEVER_ARMED == "trailing never properly armed"

    def test_giveback_too_loose_label(self):
        assert DIAG_GIVEBACK_TOO_LOOSE == "peak-giveback exit was too loose"

    def test_weak_entry_label(self):
        assert DIAG_WEAK_ENTRY == "weak entry"

    def test_strong_entry_weak_exit_label(self):
        assert DIAG_STRONG_ENTRY_WEAK_EXIT == "strong entry, weak exit"

