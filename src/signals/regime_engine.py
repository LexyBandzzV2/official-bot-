"""Regime classification engine — Phase 11.

Classifies the current market environment from measurable price-action
features extracted from a Heikin-Ashi DataFrame.  Classification is:

* Deterministic — no LLM or stochastic components.
* Fail-open — returns RegimeLabel.UNKNOWN when evidence is too thin.
* News-optional — accepts an external news flag but never fails without it.

Key outputs
-----------
classify(ha_df, ...) → RegimeSnapshot

The snapshot carries:
* regime_label        — one of the seven RegimeLabel values
* confidence_score    — 0.0–1.0 composite evidence score
* evidence_summary    — human-readable rationale
* volatility_metrics  — ATR / range / noise features
* trend_metrics       — HH/LL streak, follow-through, breakout-fail rate
* chop_metrics        — choppiness index, compression, body quality
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

log = logging.getLogger(__name__)

try:
    from src.signals.regime_types import (
        RegimeLabel, RegimeSnapshot,
        VolatilityMetrics, TrendMetrics, ChopMetrics,
    )
except ImportError as exc:
    raise ImportError("regime_types must be importable before regime_engine") from exc

try:
    from src.signals.strategy_mode import timeframe_to_mode
except ImportError:
    def timeframe_to_mode(tf: str) -> str:  # type: ignore[misc]
        return "UNKNOWN"


# ── Constants ─────────────────────────────────────────────────────────────────

# Minimum number of candles required for a classification attempt.
_MIN_CANDLES: int = 20

# Choppiness index thresholds (0–1 normalised version)
_CHOP_TREND_THRESHOLD: float = 0.382    # below this → trending
_CHOP_CHOP_THRESHOLD:  float = 0.618    # above this → choppy

# ATR expansion threshold: atr_ratio > 1 + this is "high vol"
_VOL_HIGH_RATIO:  float = 1.20
# ATR compression threshold: atr_ratio < 1 − this is "compressed"
_VOL_LOW_RATIO:   float = 0.85

# Trend persistence: fraction of directional bars to call it a trend
_TREND_DIR_THRESHOLD: float = 0.60

# Reversal detection: if HH/LL streak recently crossed zero
_REVERSAL_MIN_STREAK_CHANGE: int = 2

# Body quality for chop / trend
_BODY_WEAK_THRESHOLD:   float = 0.35   # mean body ratio below this → choppy signal
_BODY_STRONG_THRESHOLD: float = 0.55   # above this → trend signal

# News instability: when flagged true, minimum confidence for NEWS_DRIVEN_UNSTABLE
_NEWS_UNSTABLE_MIN_CONF: float = 0.55

# Minimum confidence gates per label to prevent low-data false positives
_MIN_CONF_TO_LABEL: float = 0.35   # below → emit UNKNOWN


# ── Feature extraction helpers ────────────────────────────────────────────────

def _safe_col(df: Any, *names: str) -> Optional[Any]:
    """Return the first matching column Series or None."""
    try:
        for n in names:
            if n in df.columns:
                return df[n]
    except Exception:
        pass
    return None


def _extract_volatility_metrics(ha_df: Any, window: int) -> VolatilityMetrics:
    """Compute ATR-based and range-based volatility features."""
    vm = VolatilityMetrics()
    try:
        import numpy as np

        close = _safe_col(ha_df, "ha_close", "close")
        high  = _safe_col(ha_df, "ha_high",  "high")
        low   = _safe_col(ha_df, "ha_low",   "low")
        if close is None or high is None or low is None:
            return vm

        recent = ha_df.tail(window)
        r_close = recent[close.name].values.astype(float)
        r_high  = recent[high.name].values.astype(float)
        r_low   = recent[low.name].values.astype(float)
        ranges  = r_high - r_low

        # ATR proxy: mean of ranges over 14 bars vs lookback mean
        atr_col = _safe_col(ha_df, "atr_14")
        if atr_col is not None:
            atr_vals = ha_df.tail(window)[atr_col.name].dropna().values.astype(float)
            if len(atr_vals) >= 2:
                vm.atr_current    = float(atr_vals[-1])
                lookback_mean     = float(np.mean(atr_vals[:-1])) or 1e-9
                vm.atr_ratio      = vm.atr_current / lookback_mean
                # Percentile rank of latest atr in the window
                rank = float(np.sum(atr_vals < atr_vals[-1])) / max(len(atr_vals) - 1, 1)
                vm.atr_percentile = round(rank, 4)
        else:
            # Fallback: range-based ATR proxy
            if len(ranges) >= 2:
                vm.atr_current    = float(ranges[-1])
                lookback_mean     = float(np.mean(ranges[:-1])) or 1e-9
                vm.atr_ratio      = vm.atr_current / lookback_mean
                rank = float(np.sum(ranges < ranges[-1])) / max(len(ranges) - 1, 1)
                vm.atr_percentile = round(rank, 4)

        # Range expansion vs older half of window
        half = max(len(ranges) // 2, 1)
        old_mean  = float(np.mean(ranges[:half])) or 1e-9
        new_mean  = float(np.mean(ranges[half:]))
        vm.range_expansion = round(new_mean / old_mean, 4)

        # Noise ratio: wick fraction
        bodies     = np.abs(r_close - np.where(r_close >= r_low, r_close, r_close))
        body_cols  = _safe_col(ha_df, "ha_open", "open")
        if body_cols is not None:
            opens    = recent[body_cols.name].values.astype(float)
            bodies   = np.abs(r_close - opens)
        wicks = np.where(ranges > 0, 1.0 - (bodies / np.maximum(ranges, 1e-9)), 0.0)
        vm.noise_ratio = round(float(np.mean(np.clip(wicks, 0, 1))), 4)

    except Exception as exc:
        log.debug("_extract_volatility_metrics failed: %s", exc)
    return vm


def _extract_trend_metrics(ha_df: Any, window: int) -> TrendMetrics:
    """Count HH/LL streaks and directional follow-through."""
    tm = TrendMetrics()
    try:
        import numpy as np

        close_col = _safe_col(ha_df, "ha_close", "close")
        high_col  = _safe_col(ha_df, "ha_high",  "high")
        low_col   = _safe_col(ha_df, "ha_low",   "low")
        if close_col is None:
            return tm

        recent = ha_df.tail(window)
        closes = recent[close_col.name].values.astype(float)
        highs  = recent[high_col.name].values.astype(float) if high_col is not None else closes.copy()
        lows   = recent[low_col.name].values.astype(float)  if low_col  is not None else closes.copy()

        if len(closes) < 4:
            return tm

        # HH/LL streak (positive = bullish structure, negative = bearish)
        streak = 0
        for i in range(1, len(closes)):
            if highs[i] > highs[i - 1] and lows[i] > lows[i - 1]:
                streak = max(streak + 1, 1)
            elif highs[i] < highs[i - 1] and lows[i] < lows[i - 1]:
                streak = min(streak - 1, -1)
            else:
                streak = 0  # swingback resets
        tm.hh_ll_streak = streak

        # Directional bars: fraction moving in dominant direction
        moves = np.diff(closes)
        bullish_cnt = int(np.sum(moves > 0))
        bearish_cnt = int(np.sum(moves < 0))
        dominant    = max(bullish_cnt, bearish_cnt)
        tm.directional_bars = round(dominant / max(len(moves), 1), 4)

        # Follow-through: fraction where next bar continues same direction
        if len(moves) >= 2:
            same = int(np.sum(np.sign(moves[:-1]) == np.sign(moves[1:])))
            tm.follow_through = round(same / max(len(moves) - 1, 1), 4)

        # Breakout-fail rate: fraction of local-high breaks that reversed same session
        # Simplified: bar N breaks recent high, bar N+1 closes below it
        fails = 0
        attempts = 0
        roll_high = np.maximum.accumulate(highs)
        for i in range(1, len(highs) - 1):
            if highs[i] > roll_high[i - 1]:   # breakout bar
                attempts += 1
                if closes[i + 1] < closes[i]:  # next bar reverses
                    fails += 1
        tm.breakout_fail_rate = round(fails / max(attempts, 1), 4) if attempts else 0.0

        # Composite trend strength (0–1)
        t = (
            0.35 * tm.directional_bars
            + 0.35 * tm.follow_through
            + 0.30 * (1.0 - tm.breakout_fail_rate)
        )
        tm.trend_strength = round(float(np.clip(t, 0.0, 1.0)), 4)

    except Exception as exc:
        log.debug("_extract_trend_metrics failed: %s", exc)
    return tm


def _extract_chop_metrics(ha_df: Any, window: int) -> ChopMetrics:
    """Compute choppiness index and related range-bound features."""
    cm = ChopMetrics()
    try:
        import numpy as np

        close_col = _safe_col(ha_df, "ha_close", "close")
        high_col  = _safe_col(ha_df, "ha_high",  "high")
        low_col   = _safe_col(ha_df, "ha_low",   "low")
        open_col  = _safe_col(ha_df, "ha_open",  "open")
        if close_col is None or high_col is None or low_col is None:
            return cm

        recent = ha_df.tail(window)
        closes = recent[close_col.name].values.astype(float)
        highs  = recent[high_col.name].values.astype(float)
        lows   = recent[low_col.name].values.astype(float)

        if len(closes) < 4:
            return cm

        # True ranges
        prev_closes = np.concatenate(([closes[0]], closes[:-1]))
        tr = np.maximum(highs - lows, np.maximum(
            np.abs(highs - prev_closes), np.abs(lows - prev_closes),
        ))
        total_range = float(np.max(highs) - np.min(lows))
        atr_sum     = float(np.sum(tr))
        n           = len(tr)
        if total_range > 0 and n > 1:
            # Choppiness Index normalised to 0–1 (1 = pure chop, 0 = pure trend)
            log_tr_sum = float(np.log(atr_sum + 1e-9))
            log_range  = float(np.log(total_range + 1e-9))
            log_n      = float(np.log(n))
            raw_ci     = (log_tr_sum - log_range) / max(log_n, 1e-9)
            cm.choppiness_index = round(float(np.clip(raw_ci, 0.0, 1.0)), 4)
        else:
            cm.choppiness_index = 0.5  # neutral fallback

        # Range compression: recent quarter ATR vs full window ATR
        quarter = max(len(tr) // 4, 1)
        old_atr = float(np.mean(tr[:quarter])) or 1e-9
        new_atr = float(np.mean(tr[-quarter:]))
        cm.range_compression = round(new_atr / old_atr, 4)

        # Body quality (mean body-to-range ratio)
        if open_col is not None:
            opens  = recent[open_col.name].values.astype(float)
            bodies = np.abs(closes - opens)
            ranges = highs - lows + 1e-9
            cm.body_quality_mean = round(float(np.mean(np.clip(bodies / ranges, 0, 1))), 4)

        # Reversal count (direction changes)
        moves = np.diff(closes)
        changes = int(np.sum(np.diff(np.sign(moves)) != 0))
        cm.reversal_count = changes

    except Exception as exc:
        log.debug("_extract_chop_metrics failed: %s", exc)
    return cm


# ── Decision matrix ───────────────────────────────────────────────────────────

def _build_evidence_summary(
    label: RegimeLabel,
    vm: VolatilityMetrics,
    tm: TrendMetrics,
    cm: ChopMetrics,
    news_flag: bool,
    confidence: float,
) -> str:
    """Compose a human-readable one-liner describing why a label was chosen."""
    parts: list[str] = [f"[{label.value}  conf={confidence:.2f}]"]

    if label.is_trending():
        parts.append(f"trend_strength={tm.trend_strength:.2f}")
        parts.append(f"directional_bars={tm.directional_bars:.0%}")
        if label.is_high_vol():
            parts.append(f"atr_ratio={vm.atr_ratio:.2f} (expanded)")
        else:
            parts.append(f"atr_ratio={vm.atr_ratio:.2f} (moderate)")
    elif label.is_choppy():
        parts.append(f"chop_index={cm.choppiness_index:.2f}")
        parts.append(f"body_quality={cm.body_quality_mean:.2f}")
        parts.append(f"reversals={cm.reversal_count}")
        if label.is_high_vol():
            parts.append(f"atr_ratio={vm.atr_ratio:.2f} (expanded)")
    elif label is RegimeLabel.REVERSAL_TRANSITION:
        parts.append(f"hh_ll_streak={tm.hh_ll_streak}")
        parts.append(f"breakout_fail=%={tm.breakout_fail_rate:.0%}")
        parts.append(f"chop_index={cm.choppiness_index:.2f}")
    elif label is RegimeLabel.NEWS_DRIVEN_UNSTABLE:
        parts.append("news_instability_flag=True")
        parts.append(f"noise_ratio={vm.noise_ratio:.2f}")
    elif label is RegimeLabel.UNKNOWN:
        parts.append("insufficient_evidence")

    return "  ".join(parts)


def _vote_label(
    vm: VolatilityMetrics,
    tm: TrendMetrics,
    cm: ChopMetrics,
    news_flag: bool,
) -> tuple[RegimeLabel, float]:
    """
    Deterministic voting matrix.

    Returns (label, confidence_score 0–1).
    Each feature family casts a weighted vote; the winning coalition
    determines the label and the fraction of total weight collected
    becomes the confidence score.
    """
    # ── News gate (highest priority) ─────────────────────────────────────────
    # Only emit NEWS_DRIVEN_UNSTABLE when the flag is set AND volatility is elevated
    if news_flag and vm.noise_ratio > 0.55 and vm.atr_ratio > _VOL_HIGH_RATIO:
        conf = min(0.55 + 0.25 * (vm.atr_ratio - _VOL_HIGH_RATIO), 0.90)
        return RegimeLabel.NEWS_DRIVEN_UNSTABLE, round(conf, 4)

    # ── Reversal transition detection ─────────────────────────────────────────
    # Recent streak changed sign AND choppiness is elevated AND breakout failing
    if (
        abs(tm.hh_ll_streak) <= _REVERSAL_MIN_STREAK_CHANGE
        and cm.choppiness_index > _CHOP_CHOP_THRESHOLD * 0.85
        and tm.breakout_fail_rate > 0.45
    ):
        votes = (
            0.40 * (1.0 - abs(tm.hh_ll_streak) / (_REVERSAL_MIN_STREAK_CHANGE + 1))
            + 0.35 * min(tm.breakout_fail_rate / 0.70, 1.0)
            + 0.25 * min(cm.choppiness_index, 1.0)
        )
        return RegimeLabel.REVERSAL_TRANSITION, round(float(votes), 4)

    # ── Trend / chop vote ──────────────────────────────────────────────────────
    is_high_vol = vm.atr_ratio > _VOL_HIGH_RATIO or vm.atr_percentile > 0.70

    trend_vote = (
        0.35 * tm.trend_strength
        + 0.30 * tm.directional_bars
        + 0.20 * tm.follow_through
        + 0.15 * (1.0 - cm.choppiness_index)
    )
    chop_vote = (
        0.35 * cm.choppiness_index
        + 0.30 * (1.0 - tm.trend_strength)
        + 0.20 * (1.0 - tm.directional_bars)
        + 0.15 * min(vm.noise_ratio, 1.0)
    )

    # Additional body-quality evidence
    if cm.body_quality_mean < _BODY_WEAK_THRESHOLD:
        chop_vote  += 0.10
        trend_vote -= 0.05
    elif cm.body_quality_mean > _BODY_STRONG_THRESHOLD:
        trend_vote += 0.10
        chop_vote  -= 0.05

    # Clamp to [0, 1]
    trend_vote = min(max(trend_vote, 0.0), 1.0)
    chop_vote  = min(max(chop_vote,  0.0), 1.0)

    if trend_vote > chop_vote:
        label = RegimeLabel.TRENDING_HIGH_VOL if is_high_vol else RegimeLabel.TRENDING_LOW_VOL
        conf  = trend_vote
    else:
        label = RegimeLabel.CHOPPY_HIGH_VOL if is_high_vol else RegimeLabel.CHOPPY_LOW_VOL
        conf  = chop_vote

    return label, round(float(conf), 4)


# ── Public API ────────────────────────────────────────────────────────────────

def classify(
    ha_df: Any,
    asset: str      = "",
    asset_class: str = "",
    timeframe: str  = "",
    news_instability_flag: bool = False,
    news_source: Optional[str] = None,
    source_window: Optional[int] = None,
) -> RegimeSnapshot:
    """Classify current market regime from a Heikin-Ashi DataFrame.

    Parameters
    ----------
    ha_df :
        Heikin-Ashi OHLCV DataFrame.  Expected columns: ha_open, ha_close,
        ha_high, ha_low (and optionally atr_14).
    asset, asset_class, timeframe :
        Metadata attached to the snapshot for reporting / filtering.
    news_instability_flag :
        External news signal.  When True *and* price-action confirms instability
        the engine may emit NEWS_DRIVEN_UNSTABLE.  Defaults to False.
    news_source :
        Descriptive label for the news feed ("external") or None.
    source_window :
        Override for the rolling candle window (default from config).

    Returns
    -------
    RegimeSnapshot
        Always returns a snapshot.  On any failure the label is UNKNOWN and
        confidence is 0.0 so callers can safely read the result without guarding.
    """
    # Determine window length
    try:
        from src.config import REGIME_SOURCE_WINDOW
    except ImportError:
        REGIME_SOURCE_WINDOW = 50

    window = source_window or REGIME_SOURCE_WINDOW

    # Fail-safe snapshot for error paths
    _fallback = RegimeSnapshot(
        regime_id        = str(uuid.uuid4()),
        created_at       = datetime.now(timezone.utc),
        asset            = asset,
        asset_class      = asset_class,
        timeframe        = timeframe,
        strategy_mode    = timeframe_to_mode(timeframe),
        regime_label     = RegimeLabel.UNKNOWN,
        confidence_score = 0.0,
        evidence_summary = "classification_failed",
        news_instability_flag = news_instability_flag,
        news_source      = news_source,
        source_window    = window,
    )

    try:
        # Guard: need enough candles
        if ha_df is None:
            _fallback.evidence_summary = "no_data_available"
            return _fallback
        try:
            n_rows = len(ha_df)
        except Exception:
            _fallback.evidence_summary = "cannot_measure_data_length"
            return _fallback

        if n_rows < _MIN_CANDLES:
            _fallback.evidence_summary = f"insufficient_candles ({n_rows} < {_MIN_CANDLES})"
            return _fallback

        # Log news input availability
        log.debug(
            "regime classify: asset=%s tf=%s window=%d news_input=%s",
            asset, timeframe, window, "yes" if news_instability_flag else "no",
        )

        # Feature extraction
        vm = _extract_volatility_metrics(ha_df, window)
        tm = _extract_trend_metrics(ha_df, window)
        cm = _extract_chop_metrics(ha_df, window)

        # Decision matrix
        label, conf = _vote_label(vm, tm, cm, news_instability_flag)

        try:
            from src.config import REGIME_MIN_CONFIDENCE
        except ImportError:
            REGIME_MIN_CONFIDENCE = 0.40

        # Emit UNKNOWN when evidence is too thin
        if conf < _MIN_CONF_TO_LABEL:
            label = RegimeLabel.UNKNOWN
            conf  = 0.0

        strategy_mode = timeframe_to_mode(timeframe)
        evidence = _build_evidence_summary(label, vm, tm, cm, news_instability_flag, conf)

        return RegimeSnapshot(
            regime_id        = str(uuid.uuid4()),
            created_at       = datetime.now(timezone.utc),
            asset            = asset,
            asset_class      = asset_class,
            timeframe        = timeframe,
            strategy_mode    = strategy_mode,
            regime_label     = label,
            confidence_score = conf,
            evidence_summary = evidence,
            volatility_metrics     = vm,
            trend_metrics          = tm,
            chop_metrics           = cm,
            news_instability_flag  = news_instability_flag,
            news_source            = news_source,
            source_window          = window,
        )

    except Exception as exc:
        log.warning("regime_engine.classify failed for %s/%s: %s", asset, timeframe, exc)
        _fallback.evidence_summary = f"classification_error: {exc}"
        return _fallback


def should_persist(
    new_snapshot: RegimeSnapshot,
    prev_snapshot: Optional[RegimeSnapshot],
) -> bool:
    """Decide whether *new_snapshot* should be written to the database.

    Persistence is triggered when:
    * No previous snapshot exists for this (asset, timeframe).
    * The regime label has changed.
    * The confidence has shifted by more than REGIME_CHANGE_CONFIDENCE_DELTA.
    * The previous label was UNKNOWN and the new label is known.

    This keeps DB writes low when the market environment is stable.
    """
    if prev_snapshot is None:
        return True
    if prev_snapshot.regime_label != new_snapshot.regime_label:
        return True
    if prev_snapshot.regime_label is RegimeLabel.UNKNOWN and new_snapshot.regime_label is not RegimeLabel.UNKNOWN:
        return True

    try:
        from src.config import REGIME_CHANGE_CONFIDENCE_DELTA
    except ImportError:
        REGIME_CHANGE_CONFIDENCE_DELTA = 0.15

    delta = abs(new_snapshot.confidence_score - prev_snapshot.confidence_score)
    return delta >= REGIME_CHANGE_CONFIDENCE_DELTA
