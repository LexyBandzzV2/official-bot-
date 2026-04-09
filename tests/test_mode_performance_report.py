"""Tests for the strategy-mode performance comparison report — Prompt 8.

Covers:
  A. compute_mode_stats  — all metrics from synthetic trade dicts
  B. compute_conclusions — best/leakage/efficiency from known data
  C. compute_all_modes   — mode grouping and zero-mode handling
  D. Markdown output     — structural and content checks
  E. JSON output         — schema stability and value accuracy
  F. Edge cases          — empty/sparse/None fields, duration parsing
  G. DB integration      — monkeypatched SQLITE_PATH with init_db
  H. Duration helpers    — string and datetime parsing, formatting
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.tools.mode_performance_report import (
    _duration_secs,
    _fmt_duration,
    _normalize_reason,
    _parse_dt,
    compute_all_modes,
    compute_conclusions,
    compute_mode_stats,
    get_mode_performance_data,
    mode_performance_to_json,
    mode_performance_to_markdown,
    print_mode_performance_report,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trade(
    *,
    mode: str              = "SCALP",
    pnl: float             = 1.0,
    mfe: float             = 2.0,
    mae: float             = -0.5,
    protected: bool        = False,
    be_armed: bool         = False,
    pl_stage: int          = 0,
    close_reason: str      = "PEAK_GIVEBACK_EXIT",
    entry_time: str | None = "2025-01-01T10:00:00",
    exit_time:  str | None = "2025-01-01T12:30:00",
) -> dict:
    return {
        "trade_id":              str(uuid.uuid4()),
        "asset":                 "BTCUSDT",
        "strategy_mode":         mode,
        "pnl_pct":               pnl,
        "max_unrealized_profit": mfe,
        "min_unrealized_profit": mae,
        "was_protected_profit":  protected,
        "break_even_armed":      be_armed,
        "profit_lock_stage":     pl_stage,
        "close_reason":          close_reason,
        "entry_time":            entry_time,
        "exit_time":             exit_time,
    }


def _make_trades(n: int, **kwargs) -> list[dict]:
    return [_trade(**kwargs) for _ in range(n)]


@pytest.fixture()
def _db(tmp_path, monkeypatch):
    """Isolated DB with schema initialised."""
    db_file = str(tmp_path / "test_mode.db")
    import src.data.db as db_mod
    monkeypatch.setattr(db_mod, "SQLITE_PATH", db_file)
    db_mod._sb_client = None
    db_mod.init_db()
    return db_mod


# ─────────────────────────────────────────────────────────────────────────────
# A — compute_mode_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeModeStats:
    def test_total_trades_counted(self):
        trades = _make_trades(10, mode="SCALP", pnl=1.0)
        s = compute_mode_stats(trades)
        assert s["total_trades"] == 10

    def test_win_rate_all_winners(self):
        trades = _make_trades(8, pnl=1.0)
        s = compute_mode_stats(trades)
        assert s["win_rate"] == pytest.approx(1.0)

    def test_win_rate_all_losers(self):
        trades = _make_trades(5, pnl=-0.5)
        s = compute_mode_stats(trades)
        assert s["win_rate"] == pytest.approx(0.0)

    def test_win_rate_mixed(self):
        trades = _make_trades(6, pnl=1.0) + _make_trades(4, pnl=-0.5)
        s = compute_mode_stats(trades)
        assert s["win_rate"] == pytest.approx(0.6)

    def test_avg_realized_pnl(self):
        trades = [_trade(pnl=2.0), _trade(pnl=0.0), _trade(pnl=-1.0)]
        s = compute_mode_stats(trades)
        assert s["avg_realized_pnl"] == pytest.approx(1.0 / 3.0)

    def test_avg_mfe(self):
        trades = [_trade(mfe=3.0), _trade(mfe=1.0)]
        s = compute_mode_stats(trades)
        assert s["avg_mfe"] == pytest.approx(2.0)

    def test_avg_mae(self):
        trades = [_trade(mae=-1.0), _trade(mae=-3.0)]
        s = compute_mode_stats(trades)
        assert s["avg_mae"] == pytest.approx(-2.0)

    def test_avg_giveback_equals_mfe_minus_pnl(self):
        trades = [_trade(pnl=1.0, mfe=3.0), _trade(pnl=2.0, mfe=4.0)]
        s = compute_mode_stats(trades)
        expected = ((3.0 - 1.0) + (4.0 - 2.0)) / 2  # = 2.0
        assert s["avg_giveback"] == pytest.approx(expected)

    def test_capture_ratio_basic(self):
        trades = [_trade(pnl=1.0, mfe=2.0), _trade(pnl=3.0, mfe=4.0)]
        s = compute_mode_stats(trades)
        # (0.5 + 0.75) / 2 = 0.625
        assert s["avg_capture_ratio"] == pytest.approx(0.625)

    def test_capture_ratio_skips_zero_mfe(self):
        trades = [_trade(pnl=0.5, mfe=0.0), _trade(pnl=1.0, mfe=2.0)]
        s = compute_mode_stats(trades)
        # Only second trade counts
        assert s["avg_capture_ratio"] == pytest.approx(0.5)
        assert s["mfe_sample_count"] == 1

    def test_protected_profit_counting(self):
        trades = (
            _make_trades(3, protected=True) +
            _make_trades(7, protected=False)
        )
        s = compute_mode_stats(trades)
        assert s["protected_profit_count"] == 3
        assert s["protected_profit_rate"]  == pytest.approx(0.3)

    def test_exit_reason_distribution(self):
        trades = [
            _trade(close_reason="PEAK_GIVEBACK_EXIT"),
            _trade(close_reason="PEAK_GIVEBACK_EXIT"),
            _trade(close_reason="HARD_STOP"),
        ]
        s = compute_mode_stats(trades)
        dist = s["exit_reason_dist"]
        assert dist["PEAK_GIVEBACK_EXIT"] == 2
        assert dist["HARD_STOP"]          == 1

    def test_legacy_trailing_tp_normalised(self):
        trades = [_trade(close_reason="TRAILING_TP")]
        s = compute_mode_stats(trades)
        dist = s["exit_reason_dist"]
        # Must not appear under TRAILING_TP; must be counted as PEAK_GIVEBACK_EXIT
        assert dist.get("TRAILING_TP", 0) == 0
        assert dist["PEAK_GIVEBACK_EXIT"]  == 1

    def test_unknown_reason_binned_to_unknown(self):
        trades = [_trade(close_reason="SOME_FUTURE_REASON")]
        s = compute_mode_stats(trades)
        assert s["exit_reason_dist"]["UNKNOWN"] >= 1

    def test_avg_duration_computed(self):
        # entry 10:00 exit 12:30 = 2.5h = 9000s
        trades = [_trade(entry_time="2025-01-01T10:00:00", exit_time="2025-01-01T12:30:00")]
        s = compute_mode_stats(trades)
        assert s["avg_duration_secs"] == pytest.approx(9000.0)
        assert s["avg_duration_str"]  == "2h 30m"

    def test_avg_duration_none_when_all_timestamps_invalid(self):
        trades = [_trade(entry_time=None, exit_time=None)]
        s = compute_mode_stats(trades)
        assert s["avg_duration_secs"] is None
        assert s["avg_duration_str"]  == "—"

    def test_empty_list_returns_zero_stats(self):
        s = compute_mode_stats([])
        assert s["total_trades"]      == 0
        assert s["win_rate"]          == 0.0
        assert s["avg_realized_pnl"]  == 0.0
        assert s["avg_mfe"]           == 0.0
        assert s["avg_giveback"]      == 0.0
        assert s["avg_capture_ratio"] == 0.0
        assert s["avg_duration_str"]  == "—"

    def test_none_pnl_treated_as_zero(self):
        trades = [{"pnl_pct": None, "max_unrealized_profit": None,
                   "min_unrealized_profit": None, "was_protected_profit": None,
                   "close_reason": None, "entry_time": None, "exit_time": None}]
        s = compute_mode_stats(trades)
        assert s["total_trades"] == 1
        # Should not raise


# ─────────────────────────────────────────────────────────────────────────────
# B — compute_conclusions
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeConclusions:
    def _by_mode(
        self,
        scalp:  tuple[float, float, float, int] = (0.65, 1.5, 0.7, 0.6, 20),
        inter:  tuple[float, float, float, int] = (0.50, 0.8, 1.0, 0.4, 15),
        swing:  tuple[float, float, float, int] = (0.40, 0.2, 1.3, 0.3, 10),
    ) -> dict:
        """
        Args: (win_rate, avg_realized_pnl, avg_mfe, avg_capture_ratio, count)
        """
        def _make(t):
            wr, pnl, mfe, cap, n = t
            return {
                "total_trades":      n,
                "win_rate":          wr,
                "avg_realized_pnl":  pnl,
                "avg_mfe":           mfe,
                "avg_give_back":     mfe - pnl,
                "avg_giveback":      mfe - pnl,
                "avg_capture_ratio": cap,
                "mfe_sample_count":  n,
            }
        return {
            "SCALP":        _make(scalp),
            "INTERMEDIATE": _make(inter),
            "SWING":        _make(swing),
        }

    def test_best_mode_by_win_rate(self):
        bm = self._by_mode()
        c = compute_conclusions(bm)
        assert c["best_mode"]["mode"] == "SCALP"

    def test_best_mode_tiebreak_by_avg_pnl(self):
        bm = self._by_mode(
            scalp=(0.60, 2.0, 1.5, 0.8, 10),
            inter=(0.60, 1.0, 1.5, 0.8, 10),
            swing=(0.40, 0.5, 1.0, 0.6, 10),
        )
        c = compute_conclusions(bm)
        assert c["best_mode"]["mode"] == "SCALP"  # same win_rate, higher pnl

    def test_most_leakage_highest_giveback(self):
        bm = self._by_mode(
            scalp=(0.65, 1.5, 2.5, 0.6, 20),   # giveback 1.0
            inter=(0.50, 0.5, 3.0, 0.5, 15),   # giveback 2.5  ← highest
            swing=(0.40, 0.2, 1.5, 0.3, 10),   # giveback 1.3
        )
        c = compute_conclusions(bm)
        assert c["most_leakage_mode"]["mode"] == "INTERMEDIATE"

    def test_worst_exit_efficiency_lowest_capture(self):
        bm = self._by_mode(
            scalp=(0.65, 1.5, 2.5, 0.75, 20),
            inter=(0.50, 0.5, 3.0, 0.25, 15),  # lowest capture ← worst
            swing=(0.40, 0.2, 1.5, 0.55, 10),
        )
        c = compute_conclusions(bm)
        assert c["worst_exit_efficiency_mode"]["mode"] == "INTERMEDIATE"

    def test_best_mode_none_when_all_below_min_trades(self):
        bm = {
            "SCALP":        {"total_trades": 2, "win_rate": 0.9, "avg_realized_pnl": 5.0,
                             "avg_mfe": 6.0, "avg_giveback": 1.0, "avg_capture_ratio": 0.8, "mfe_sample_count": 2},
            "INTERMEDIATE": {"total_trades": 1, "win_rate": 0.8, "avg_realized_pnl": 4.0,
                             "avg_mfe": 5.0, "avg_giveback": 1.0, "avg_capture_ratio": 0.8, "mfe_sample_count": 1},
            "SWING":        {"total_trades": 0, "win_rate": 0.0, "avg_realized_pnl": 0.0,
                             "avg_mfe": 0.0, "avg_giveback": 0.0, "avg_capture_ratio": 0.0, "mfe_sample_count": 0},
        }
        c = compute_conclusions(bm)
        assert c["best_mode"] is None

    def test_conclusions_contain_all_three_keys(self):
        bm = self._by_mode()
        c = compute_conclusions(bm)
        assert "best_mode" in c
        assert "most_leakage_mode" in c
        assert "worst_exit_efficiency_mode" in c

    def test_conclusions_include_reason_strings(self):
        bm = self._by_mode()
        c = compute_conclusions(bm)
        assert isinstance(c["best_mode"]["reason"], str)
        assert len(c["best_mode"]["reason"]) > 10

    def test_empty_by_mode_dict_returns_all_none(self):
        c = compute_conclusions({})
        assert c["best_mode"] is None
        assert c["most_leakage_mode"] is None
        assert c["worst_exit_efficiency_mode"] is None


# ─────────────────────────────────────────────────────────────────────────────
# C — compute_all_modes
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeAllModes:
    def test_returns_all_three_modes(self):
        trades = _make_trades(5, mode="SCALP") + _make_trades(5, mode="INTERMEDIATE")
        result = compute_all_modes(trades)
        assert set(result.keys()) == {"SCALP", "INTERMEDIATE", "SWING"}

    def test_zero_count_for_missing_mode(self):
        trades = _make_trades(10, mode="SCALP")
        result = compute_all_modes(trades)
        assert result["SWING"]["total_trades"] == 0
        assert result["INTERMEDIATE"]["total_trades"] == 0

    def test_counts_grouped_correctly(self):
        trades = (
            _make_trades(6, mode="SCALP") +
            _make_trades(4, mode="INTERMEDIATE") +
            _make_trades(2, mode="SWING")
        )
        result = compute_all_modes(trades)
        assert result["SCALP"]["total_trades"]        == 6
        assert result["INTERMEDIATE"]["total_trades"] == 4
        assert result["SWING"]["total_trades"]        == 2

    def test_ignores_unknown_mode_in_trade_list(self):
        trades = _make_trades(3, mode="UNKNOWN_MODE")
        result = compute_all_modes(trades)
        # All modes should have 0 trades
        for s in result.values():
            assert s["total_trades"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# D — Markdown output
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkdownOutput:
    def _data(self) -> dict:
        from src.tools.mode_performance_report import compute_all_modes, compute_conclusions
        trades = (
            _make_trades(10, mode="SCALP",        pnl=1.5, mfe=3.0, protected=True)  +
            _make_trades(8,  mode="INTERMEDIATE", pnl=0.8, mfe=2.0)                  +
            _make_trades(5,  mode="SWING",        pnl=0.2, mfe=1.5)
        )
        by_mode = compute_all_modes(trades)
        return {
            "by_mode":     by_mode,
            "conclusions": compute_conclusions(by_mode),
            "total_closed": 23,
            "generated_at": "2025-01-01T00:00:00+00:00",
        }

    def test_contains_main_heading(self):
        md = mode_performance_to_markdown(self._data())
        assert "# Strategy Mode Performance Report" in md

    def test_contains_core_metrics_section(self):
        md = mode_performance_to_markdown(self._data())
        assert "## Core Metrics by Mode" in md

    def test_contains_all_three_modes(self):
        md = mode_performance_to_markdown(self._data())
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert mode in md

    def test_contains_exit_reason_section(self):
        md = mode_performance_to_markdown(self._data())
        assert "Exit Reason" in md

    def test_contains_conclusions_section(self):
        md = mode_performance_to_markdown(self._data())
        assert "## Conclusions" in md

    def test_contains_contextual_observations(self):
        md = mode_performance_to_markdown(self._data())
        assert "Contextual Observations" in md

    def test_markdown_is_string(self):
        md = mode_performance_to_markdown(self._data())
        assert isinstance(md, str)
        assert len(md) > 100

    def test_total_closed_shown_in_header(self):
        md = mode_performance_to_markdown(self._data())
        assert "23" in md

    def test_empty_data_does_not_raise(self):
        data = {
            "by_mode":     compute_all_modes([]),
            "conclusions": compute_conclusions(compute_all_modes([])),
            "total_closed": 0,
            "generated_at": "2025-01-01T00:00:00",
        }
        md = mode_performance_to_markdown(data)
        assert "# Strategy Mode Performance Report" in md


# ─────────────────────────────────────────────────────────────────────────────
# E — JSON output
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonOutput:
    def _data(self) -> dict:
        trades = (
            _make_trades(8, mode="SCALP",        pnl=1.2, mfe=2.5) +
            _make_trades(6, mode="INTERMEDIATE", pnl=0.5, mfe=1.8) +
            _make_trades(5, mode="SWING",        pnl=0.1, mfe=1.0)
        )
        by_mode = compute_all_modes(trades)
        return {
            "by_mode":      by_mode,
            "conclusions":  compute_conclusions(by_mode),
            "total_closed": 19,
            "generated_at": "2025-01-01T00:00:00+00:00",
        }

    def test_returns_valid_json(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        assert isinstance(decoded, dict)

    def test_top_level_keys_present(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        for key in ("by_mode", "conclusions", "total_closed", "generated_at"):
            assert key in decoded, f"Missing key: {key}"

    def test_by_mode_has_all_three_modes(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        for mode in ("SCALP", "INTERMEDIATE", "SWING"):
            assert mode in decoded["by_mode"]

    def test_mode_stats_keys_complete(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        scalp = decoded["by_mode"]["SCALP"]
        for key in (
            "total_trades", "win_rate", "avg_realized_pnl", "avg_mfe",
            "avg_mae", "avg_giveback", "avg_capture_ratio", "mfe_sample_count",
            "protected_profit_count", "protected_profit_rate",
            "exit_reason_dist", "avg_duration_str",
        ):
            assert key in scalp, f"Missing key in SCALP stats: {key}"

    def test_conclusions_keys_present(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        for key in ("best_mode", "most_leakage_mode", "worst_exit_efficiency_mode"):
            assert key in decoded["conclusions"]

    def test_numeric_values_are_rounded(self):
        js = mode_performance_to_json(self._data())
        decoded = json.loads(js)
        win_rate = decoded["by_mode"]["SCALP"]["win_rate"]
        # Should have at most 4 decimal places
        assert win_rate == round(win_rate, 4)

    def test_empty_data_produces_valid_json(self):
        data = {
            "by_mode":     compute_all_modes([]),
            "conclusions": compute_conclusions(compute_all_modes([])),
            "total_closed": 0,
            "generated_at": "2025-01-01T00:00:00",
        }
        js = mode_performance_to_json(data)
        decoded = json.loads(js)
        assert decoded["total_closed"] == 0
        assert decoded["conclusions"]["best_mode"] is None


# ─────────────────────────────────────────────────────────────────────────────
# F — Edge cases (None/sparse/unusual inputs)
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_single_trade_returns_valid_stats(self):
        s = compute_mode_stats([_trade(pnl=1.0, mfe=2.0)])
        assert s["total_trades"]     == 1
        assert s["win_rate"]         == pytest.approx(1.0)
        assert s["avg_capture_ratio"]== pytest.approx(0.5)

    def test_trade_with_zero_mfe_excluded_from_capture(self):
        s = compute_mode_stats([_trade(pnl=0.5, mfe=0.0)])
        assert s["avg_capture_ratio"] == 0.0
        assert s["mfe_sample_count"]  == 0

    def test_negative_pnl_counted_correctly(self):
        trades = [_trade(pnl=-2.0, mfe=1.0), _trade(pnl=3.0, mfe=4.0)]
        s = compute_mode_stats(trades)
        assert s["win_rate"] == pytest.approx(0.5)
        # capture: (-2/1 + 3/4) / 2 = (-2 + 0.75) / 2 = -0.625
        assert s["avg_capture_ratio"] == pytest.approx((-2.0 + 0.75) / 2)

    def test_duration_missing_one_timestamp(self):
        trades = [_trade(entry_time="2025-01-01T10:00:00", exit_time=None)]
        s = compute_mode_stats(trades)
        assert s["avg_duration_secs"] is None

    def test_duration_averaged_across_multiple(self):
        # 1h = 3600s and 2h = 7200s → avg = 5400s = 1h 30m
        trades = [
            _trade(entry_time="2025-01-01T10:00:00", exit_time="2025-01-01T11:00:00"),
            _trade(entry_time="2025-01-01T12:00:00", exit_time="2025-01-01T14:00:00"),
        ]
        s = compute_mode_stats(trades)
        assert s["avg_duration_secs"] == pytest.approx(5400.0)
        assert s["avg_duration_str"]  == "1h 30m"

    def test_all_none_fields_in_row_does_not_raise(self):
        trade = {k: None for k in (
            "trade_id", "asset", "strategy_mode", "pnl_pct",
            "max_unrealized_profit", "min_unrealized_profit",
            "was_protected_profit", "break_even_armed", "profit_lock_stage",
            "close_reason", "entry_time", "exit_time",
        )}
        s = compute_mode_stats([trade])
        assert s["total_trades"] == 1

    def test_print_mode_performance_report_on_empty_data_does_not_raise(self):
        data = {
            "by_mode":     compute_all_modes([]),
            "conclusions": compute_conclusions(compute_all_modes([])),
            "total_closed": 0,
            "generated_at": "2025-01-01T00:00:00",
        }
        # Should not raise; output to /dev/null via StringIO capture
        try:
            from rich.console import Console
            from io import StringIO
            con = Console(file=StringIO(), no_color=True)
            print_mode_performance_report(data, console=con)
        except ImportError:
            print_mode_performance_report(data)  # plain fallback


# ─────────────────────────────────────────────────────────────────────────────
# G — DB integration (monkeypatched SQLITE_PATH)
# ─────────────────────────────────────────────────────────────────────────────

class TestDbIntegration:
    def _insert_closed_trade(self, conn: sqlite3.Connection, *, mode: str, pnl: float):
        conn.execute(
            """
            INSERT INTO trades
              (trade_id, asset, timeframe, signal_type, entry_price, entry_time,
               stop_loss_hard, trailing_stop, position_size, account_risk_pct,
               exit_price, exit_time, pnl_pct, status,
               strategy_mode, close_reason,
               max_unrealized_profit, min_unrealized_profit)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), "BTCUSDT", "5m", "BUY", 100.0,
                "2025-01-01T10:00:00",
                95.0, 97.0, 1.0, 1.0,
                101.0, "2025-01-01T12:00:00",
                pnl, "CLOSED",
                mode, "PEAK_GIVEBACK_EXIT",
                pnl + 0.5, -0.2,
            ),
        )
        conn.commit()

    def test_get_mode_performance_data_empty_db(self, tmp_path, _db):
        data = get_mode_performance_data(db_path=_db.SQLITE_PATH)
        assert isinstance(data, dict)
        assert data["total_closed"] == 0
        assert "by_mode" in data
        assert "conclusions" in data

    def test_get_mode_performance_data_with_trades(self, tmp_path, _db):
        with _db._sqlite_conn() as conn:
            for pnl in [1.0, 2.0, -0.5]:
                self._insert_closed_trade(conn, mode="SCALP", pnl=pnl)
            for pnl in [0.5, 0.8]:
                self._insert_closed_trade(conn, mode="INTERMEDIATE", pnl=pnl)

        data = get_mode_performance_data(db_path=_db.SQLITE_PATH)
        assert data["by_mode"]["SCALP"]["total_trades"]        == 3
        assert data["by_mode"]["INTERMEDIATE"]["total_trades"] == 2
        assert data["by_mode"]["SWING"]["total_trades"]        == 0

    def test_get_mode_performance_data_computed_win_rate(self, tmp_path, _db):
        with _db._sqlite_conn() as conn:
            for pnl in [1.0, 1.0, -0.5, 1.0, -0.5]:
                self._insert_closed_trade(conn, mode="SCALP", pnl=pnl)

        data = get_mode_performance_data(db_path=_db.SQLITE_PATH)
        # 3 wins out of 5 = 0.6
        assert data["by_mode"]["SCALP"]["win_rate"] == pytest.approx(0.6)

    def test_generated_at_is_populated(self, tmp_path, _db):
        data = get_mode_performance_data(db_path=_db.SQLITE_PATH)
        assert "generated_at" in data
        assert isinstance(data["generated_at"], str)
        assert len(data["generated_at"]) > 10


# ─────────────────────────────────────────────────────────────────────────────
# H — Duration and datetime helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestDurationHelpers:
    def test_parse_dt_iso_string(self):
        dt = _parse_dt("2025-01-01T10:00:00")
        assert isinstance(dt, datetime)
        assert dt.year == 2025

    def test_parse_dt_with_tz_offset(self):
        dt = _parse_dt("2025-01-01T10:00:00+00:00")
        assert dt is not None

    def test_parse_dt_with_z_suffix(self):
        dt = _parse_dt("2025-01-01T10:00:00Z")
        assert dt is not None

    def test_parse_dt_returns_none_on_empty_string(self):
        assert _parse_dt("") is None

    def test_parse_dt_returns_none_on_none(self):
        assert _parse_dt(None) is None

    def test_parse_dt_passthrough_datetime(self):
        dt_in = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        dt_out = _parse_dt(dt_in)
        assert dt_out is dt_in

    def test_duration_secs_basic(self):
        secs = _duration_secs("2025-01-01T10:00:00", "2025-01-01T11:00:00")
        assert secs == pytest.approx(3600.0)

    def test_duration_secs_none_on_missing(self):
        assert _duration_secs(None, "2025-01-01T12:00:00") is None
        assert _duration_secs("2025-01-01T10:00:00", None) is None

    def test_fmt_duration_hours_and_mins(self):
        assert _fmt_duration(5400.0)  == "1h 30m"
        assert _fmt_duration(9000.0)  == "2h 30m"
        assert _fmt_duration(3660.0)  == "1h 1m"

    def test_fmt_duration_zero(self):
        assert _fmt_duration(0.0) == "0h 0m"

    def test_fmt_duration_none_returns_dash(self):
        assert _fmt_duration(None) == "—"

    def test_normalize_reason_trailing_tp(self):
        assert _normalize_reason("TRAILING_TP") == "PEAK_GIVEBACK_EXIT"

    def test_normalize_reason_known_reasons(self):
        for r in ("PEAK_GIVEBACK_EXIT", "HARD_STOP", "TRAIL_STOP", "ALLIGATOR_TP", "MANUAL"):
            assert _normalize_reason(r) == r

    def test_normalize_reason_unknown(self):
        assert _normalize_reason("SOME_FUTURE_REASON") == "UNKNOWN"

    def test_normalize_reason_none(self):
        assert _normalize_reason(None) == "UNKNOWN"

    def test_normalize_reason_empty_string(self):
        assert _normalize_reason("") == "UNKNOWN"
