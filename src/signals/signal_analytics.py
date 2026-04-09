"""Signal quality analytics — Phase 5.

Reads ``buy_signals`` / ``sell_signals`` tables and produces per-mode,
per-asset, and per-combination analytics so you can answer:

  - Which modes accept the highest fraction of signals?
  - Which assets consistently score above threshold?
  - What rejection reasons fire most often?
  - Is ML blocking too many near-miss signals?
  - Which indicator combinations have the best accept rate?

All functions accept a *db_path* (str or Path) and a *signal_type*
(``"BUY"`` or ``"SELL"``).  They return plain Python dicts / lists — no
Rich or display dependency — so they can be used programmatically or
plugged into reporter.py.

Near-miss definition
--------------------
A signal where ``is_valid = 1`` and ``accepted_signal = 0`` with a
``score_total`` in the half-open interval ``[bound_low, bound_high)``.
The defaults (60, 70) mirror the user's "60–69 near-miss" language when
the threshold is 70.

Backward compatibility
----------------------
Old rows pre-Phase-5 have score_total = 0.0 and accepted_signal = 0.
Analytics that aggregate scores will return 0.0 for those rows.
Functions that filter on accepted_signal exclude them from accept-rate
numerators correctly (old accepted trades are already in the trades table,
not the signals table).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MODES  = ("SCALP", "INTERMEDIATE", "SWING")
_SIGNAL_TABLES = {"BUY": "buy_signals", "SELL": "sell_signals"}


def _conn(db_path: Any) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def _table(signal_type: str) -> str:
    return _SIGNAL_TABLES.get(signal_type.upper(), "buy_signals")


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ── Public analytics ─────────────────────────────────────────────────────────

def accepted_vs_rejected_by_mode(
    db_path: Any,
    signal_type: str = "BUY",
) -> dict[str, dict]:
    """Return accepted / rejected counts and average score by strategy mode.

    Returns
    -------
    dict mapping each mode (``"SCALP"``, ``"INTERMEDIATE"``, ``"SWING"``) to::

        {
            "accepted":    int,
            "rejected":    int,
            "total":       int,
            "accept_rate": float,   # 0.0–1.0
            "avg_score":   float,   # average score_total across all signals
        }
    """
    tbl = _table(signal_type)
    result: dict[str, dict] = {}
    try:
        conn = _conn(db_path)
        try:
            for mode in _MODES:
                rows = conn.execute(
                    f"SELECT accepted_signal, score_total FROM {tbl}"
                    " WHERE strategy_mode = ?",
                    (mode,),
                ).fetchall()
                accepted = sum(1 for r in rows if r["accepted_signal"])
                total    = len(rows)
                scores   = [float(r["score_total"]) for r in rows]
                result[mode] = {
                    "accepted":    accepted,
                    "rejected":    total - accepted,
                    "total":       total,
                    "accept_rate": accepted / total if total else 0.0,
                    "avg_score":   _safe_mean(scores),
                }
        finally:
            conn.close()
    except Exception as exc:
        log.warning("accepted_vs_rejected_by_mode failed: %s", exc)
        for mode in _MODES:
            result.setdefault(mode, {"accepted": 0, "rejected": 0, "total": 0,
                                     "accept_rate": 0.0, "avg_score": 0.0})
    return result


def avg_score_by_asset(
    db_path: Any,
    signal_type: str = "BUY",
    min_count: int = 3,
) -> list[dict]:
    """Return average score and accept rate ranked by average score descending.

    Parameters
    ----------
    min_count:
        Minimum number of signals for an asset to appear in the results.
        Filters out assets with very few data points.

    Returns
    -------
    list of dicts::

        [{"asset": str, "mode": str, "avg_score": float,
          "accept_rate": float, "count": int}, ...]

    Ordered by ``avg_score`` descending.
    """
    tbl = _table(signal_type)
    rows_out: list[dict] = []
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT asset, strategy_mode,
                       COUNT(*)               AS total,
                       AVG(score_total)       AS avg_score,
                       SUM(accepted_signal)   AS accepted
                FROM {tbl}
                GROUP BY asset, strategy_mode
                HAVING COUNT(*) >= ?
                ORDER BY avg_score DESC
                """,
                (min_count,),
            ).fetchall()
            for r in rows:
                total = r["total"] or 1
                rows_out.append({
                    "asset":       r["asset"],
                    "mode":        r["strategy_mode"],
                    "avg_score":   float(r["avg_score"] or 0.0),
                    "accept_rate": float(r["accepted"] or 0) / total,
                    "count":       r["total"],
                })
        finally:
            conn.close()
    except Exception as exc:
        log.warning("avg_score_by_asset failed: %s", exc)
    return rows_out


def top_rejection_reasons(
    db_path: Any,
    limit: int = 10,
    signal_type: str = "BUY",
) -> list[dict]:
    """Return the most common rejection reasons across all modes.

    Returns
    -------
    list of dicts (up to *limit* items)::

        [{"reason": str, "count": int, "modes_affected": list[str]}, ...]

    Ordered by count descending.
    """
    tbl = _table(signal_type)
    rows_out: list[dict] = []
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT rejection_reason, COUNT(*) AS cnt
                FROM {tbl}
                WHERE rejection_reason IS NOT NULL AND rejection_reason != ''
                  AND accepted_signal = 0
                GROUP BY rejection_reason
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for r in rows:
                reason = r["rejection_reason"]
                modes_rows = conn.execute(
                    f"SELECT DISTINCT strategy_mode FROM {tbl}"
                    " WHERE rejection_reason = ? AND accepted_signal = 0",
                    (reason,),
                ).fetchall()
                rows_out.append({
                    "reason": reason,
                    "count":  r["cnt"],
                    "modes_affected": [m["strategy_mode"] for m in modes_rows],
                })
        finally:
            conn.close()
    except Exception as exc:
        log.warning("top_rejection_reasons failed: %s", exc)
    return rows_out


def near_miss_signals(
    db_path: Any,
    bound_low:   float = 60.0,
    bound_high:  float = 70.0,
    signal_type: str   = "BUY",
    limit:       int   = 50,
) -> list[dict]:
    """Return signals that almost passed — valid but rejected with high score.

    Criteria: ``is_valid = 1``, ``accepted_signal = 0``,
    ``bound_low <= score_total < bound_high``.

    Returns
    -------
    list of dicts::

        [{"asset": str, "timeframe": str, "mode": str, "timestamp": str,
          "score_total": float, "rejection_reason": str,
          "ml_effect": str, "ai_effect": str,
          "indicator_flags": str}, ...]

    Ordered by ``score_total`` descending, most recent first as tie-break.
    """
    tbl = _table(signal_type)
    rows_out: list[dict] = []
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT asset, timeframe, strategy_mode, timestamp,
                       score_total, rejection_reason, ml_effect, ai_effect,
                       indicator_flags
                FROM {tbl}
                WHERE is_valid = 1
                  AND accepted_signal = 0
                  AND score_total >= ?
                  AND score_total < ?
                ORDER BY score_total DESC, timestamp DESC
                LIMIT ?
                """,
                (bound_low, bound_high, limit),
            ).fetchall()
            for r in rows:
                rows_out.append({
                    "asset":            r["asset"],
                    "timeframe":        r["timeframe"],
                    "mode":             r["strategy_mode"],
                    "timestamp":        r["timestamp"],
                    "score_total":      float(r["score_total"] or 0.0),
                    "rejection_reason": r["rejection_reason"] or "",
                    "ml_effect":        r["ml_effect"] or "",
                    "ai_effect":        r["ai_effect"] or "",
                    "indicator_flags":  r["indicator_flags"] or "",
                })
        finally:
            conn.close()
    except Exception as exc:
        log.warning("near_miss_signals failed: %s", exc)
    return rows_out


def ml_effect_summary(
    db_path: Any,
    signal_type: str = "BUY",
) -> dict:
    """Return ML (and AI) gate contribution statistics across all signals.

    Returns
    -------
    dict::

        {
            "ml_vetoed": int, "ml_passed": int, "ml_boosted": int,
            "ml_veto_rate":  float, "ml_boost_rate": float,
            "ai_vetoed": int, "ai_passed": int, "ai_boosted": int,
            "ai_veto_rate":  float, "ai_boost_rate": float,
        }
    """
    tbl = _table(signal_type)
    result: dict = {k: 0 for k in (
        "ml_vetoed", "ml_passed", "ml_boosted",
        "ai_vetoed", "ai_passed", "ai_boosted",
    )}
    result.update({"ml_veto_rate": 0.0, "ml_boost_rate": 0.0,
                   "ai_veto_rate": 0.0, "ai_boost_rate": 0.0})
    try:
        conn = _conn(db_path)
        try:
            for col_prefix in ("ml", "ai"):
                col = f"{col_prefix}_effect"
                rows = conn.execute(
                    f"SELECT {col}, COUNT(*) AS cnt FROM {tbl}"
                    f" WHERE {col} IS NOT NULL GROUP BY {col}",
                ).fetchall()
                totals: dict[str, int] = {}
                for r in rows:
                    key = (r[col] or "").lower()
                    if key in ("vetoed", "passed", "boosted"):
                        result[f"{col_prefix}_{key}"] = r["cnt"]
                        totals[key] = r["cnt"]
                grand = sum(totals.values())
                if grand:
                    result[f"{col_prefix}_veto_rate"]  = totals.get("vetoed",  0) / grand
                    result[f"{col_prefix}_boost_rate"] = totals.get("boosted", 0) / grand
        finally:
            conn.close()
    except Exception as exc:
        log.warning("ml_effect_summary failed: %s", exc)
    return result


def indicator_combination_summary(
    db_path: Any,
    signal_type: str = "BUY",
) -> list[dict]:
    """Group signals by which indicator points fired and show accept rates.

    Uses the stored boolean columns ``alligator_pt``, ``stochastic_pt``,
    ``vortex_pt`` to group, matching the existing schema.

    Returns
    -------
    list of dicts::

        [{"alligator": bool, "stochastic": bool, "vortex": bool,
          "label": str,   # e.g. "al+st+vo" or "al+vo"
          "count": int, "accept_rate": float, "avg_score": float}, ...]

    Ordered by count descending.
    """
    tbl = _table(signal_type)
    rows_out: list[dict] = []
    _abbr = {("alligator_pt", 1): "al", ("stochastic_pt", 1): "st", ("vortex_pt", 1): "vo"}
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT alligator_pt, stochastic_pt, vortex_pt,
                       COUNT(*)             AS total,
                       SUM(accepted_signal) AS accepted,
                       AVG(score_total)     AS avg_score
                FROM {tbl}
                GROUP BY alligator_pt, stochastic_pt, vortex_pt
                ORDER BY total DESC
                """,
            ).fetchall()
            for r in rows:
                al, st, vo = r["alligator_pt"], r["stochastic_pt"], r["vortex_pt"]
                parts = []
                if al: parts.append("al")
                if st: parts.append("st")
                if vo: parts.append("vo")
                total = r["total"] or 1
                rows_out.append({
                    "alligator":   bool(al),
                    "stochastic":  bool(st),
                    "vortex":      bool(vo),
                    "label":       "+".join(parts) if parts else "(none)",
                    "count":       r["total"],
                    "accept_rate": float(r["accepted"] or 0) / total,
                    "avg_score":   float(r["avg_score"] or 0.0),
                })
        finally:
            conn.close()
    except Exception as exc:
        log.warning("indicator_combination_summary failed: %s", exc)
    return rows_out


# ── Phase 14: suitability / regime-gating analytics ──────────────────────────

def skip_reason_frequency(
    db_path: Any,
    signal_type: str = "BUY",
) -> list[dict]:
    """Return skip-reason codes ranked by frequency.

    Returns
    -------
    list of ``{"skip_reason_code": str, "count": int}`` dicts, most-frequent first.
    Rows without a skip reason (empty or NULL) are omitted.
    """
    tbl = _table(signal_type)
    result: list[dict] = []
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT skip_reason_code, COUNT(*) AS cnt
                FROM {tbl}
                WHERE skip_reason_code IS NOT NULL AND skip_reason_code != ''
                GROUP BY skip_reason_code
                ORDER BY cnt DESC
                """,
            ).fetchall()
            result = [{"skip_reason_code": r[0], "count": r[1]} for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("skip_reason_frequency failed: %s", exc)
    return result


def suitability_rating_distribution(
    db_path: Any,
    signal_type: str = "BUY",
) -> dict[str, int]:
    """Return counts per suitability_rating value (HIGH/MEDIUM/LOW/BLOCKED/UNKNOWN).

    Rows with NULL suitability_rating are counted under ``"UNKNOWN"``.
    """
    tbl = _table(signal_type)
    result: dict[str, int] = {}
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT COALESCE(suitability_rating, 'UNKNOWN') AS rating,
                       COUNT(*) AS cnt
                FROM {tbl}
                GROUP BY rating
                ORDER BY cnt DESC
                """,
            ).fetchall()
            result = {r[0]: r[1] for r in rows}
        finally:
            conn.close()
    except Exception as exc:
        log.warning("suitability_rating_distribution failed: %s", exc)
    return result


def prevented_by_suitability_count(
    db_path: Any,
    signal_type: str = "BUY",
) -> int:
    """Return the number of signals hard-blocked by suitability gating."""
    tbl = _table(signal_type)
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM {tbl}
                WHERE skip_reason_code = 'blocked_by_suitability'
                """,
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception as exc:
        log.warning("prevented_by_suitability_count failed: %s", exc)
        return 0


def skipped_by_regime_summary(
    db_path: Any,
    signal_type: str = "BUY",
) -> list[dict]:
    """Return per-(macro_regime, skip_reason_code) skip counts.

    Useful for understanding which regimes generate the most gating friction.

    Returns
    -------
    list of ``{"macro_regime": str, "skip_reason_code": str, "count": int}``
    dicts, ordered by count descending.
    """
    tbl = _table(signal_type)
    result: list[dict] = []
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT COALESCE(macro_regime, 'UNKNOWN') AS macro_regime,
                       COALESCE(skip_reason_code, '') AS skip_reason_code,
                       COUNT(*) AS cnt
                FROM {tbl}
                WHERE skip_reason_code IS NOT NULL AND skip_reason_code != ''
                GROUP BY macro_regime, skip_reason_code
                ORDER BY cnt DESC
                """,
            ).fetchall()
            result = [
                {"macro_regime": r[0], "skip_reason_code": r[1], "count": r[2]}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("skipped_by_regime_summary failed: %s", exc)
    return result

