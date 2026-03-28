"""Timestamped event logger — writes to both console and log files.

Every significant event (signal, order, close, rejection, error) is written to:
    logs/signals.log  — all signal events
    logs/trades.log   — all trade open/close events
    logs/errors.log   — all errors and warnings

All timestamps use the configured timezone (America/Toronto by default).
"""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

try:
    from src.config import LOG_DIR, TIMEZONE
except ImportError:
    LOG_DIR  = Path("logs")
    TIMEZONE = "America/Toronto"

_tz = pytz.timezone(TIMEZONE)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Log format ────────────────────────────────────────────────────────────────
_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _make_file_handler(path: Path, level: int = logging.DEBUG) -> logging.FileHandler:
    h = logging.FileHandler(path, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    return h


def setup_logging() -> None:
    """Call once at startup to wire up all log handlers."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s | %(message)s"))
    root.addHandler(ch)

    # signals.log
    root.addHandler(_make_file_handler(LOG_DIR / "signals.log"))
    # trades.log
    trade_logger = logging.getLogger("trades")
    trade_logger.addHandler(_make_file_handler(LOG_DIR / "trades.log"))
    # errors.log
    err_handler = _make_file_handler(LOG_DIR / "errors.log", logging.WARNING)
    root.addHandler(err_handler)


# ── Typed event loggers ───────────────────────────────────────────────────────

_sig_log   = logging.getLogger("signals")
_trade_log = logging.getLogger("trades")
_sys_log   = logging.getLogger("system")


def log_signal(
    signal_type:      str,
    asset:            str,
    timeframe:        str,
    is_valid:         bool,
    points:           int,
    entry_price:      float,
    stop_loss:        float,
    ml_confidence:    Optional[float],
    rejection_reason: str = "",
) -> None:
    ts = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    status = "VALID" if is_valid else f"REJECTED ({rejection_reason})"
    _sig_log.info(
        "[%s] %s SIGNAL | %s %s | Points: %d/3 | Entry: %.5f | SL: %.5f | "
        "ML: %s | Status: %s",
        ts, signal_type, asset, timeframe, points,
        entry_price, stop_loss,
        f"{ml_confidence*100:.0f}%" if ml_confidence is not None else "N/A",
        status,
    )


def log_trade_open(
    trade_id:     str,
    signal_type:  str,
    asset:        str,
    timeframe:    str,
    entry_price:  float,
    stop_hard:    float,
    trail_stop:   float,
    position_size:float,
    risk_pct:     float,
) -> None:
    ts = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    _trade_log.info(
        "[%s] OPEN  | ID: %s | %s %s %s | Entry: %.5f | Hard SL: %.5f | "
        "Trail: %.5f | Size: %.4f | Risk: %.2f%%",
        ts, trade_id, signal_type, asset, timeframe,
        entry_price, stop_hard, trail_stop, position_size, risk_pct,
    )


def log_trade_close(
    trade_id:     str,
    signal_type:  str,
    asset:        str,
    entry_time:   datetime,
    exit_time:    datetime,
    entry_price:  float,
    exit_price:   float,
    close_reason: str,
    pnl:          float,
    pnl_pct:      float,
    max_trail:    float,
) -> None:
    entry_ts = entry_time.astimezone(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    exit_ts  = exit_time.astimezone(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    duration = str(exit_time - entry_time).split(".")[0]

    _trade_log.info(
        "CLOSE | ID: %s | %s %s | Entry: %s | Exit: %s | Duration: %s | "
        "Entry: %.5f | Exit: %.5f | Reason: %s | PnL: %+.2f (%+.2f%%) | "
        "MaxTrail: %.5f",
        trade_id, signal_type, asset,
        entry_ts, exit_ts, duration,
        entry_price, exit_price, close_reason,
        pnl, pnl_pct, max_trail,
    )


def log_rejection(
    signal_type: str,
    asset:       str,
    timeframe:   str,
    reason:      str,
) -> None:
    ts = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    _sig_log.warning(
        "[%s] REJECTED | %s %s %s | Reason: %s",
        ts, signal_type, asset, timeframe, reason,
    )


def log_trail_update(
    asset:     str,
    trade_id:  str,
    old_stop:  float,
    new_stop:  float,
) -> None:
    _trade_log.debug(
        "TRAIL | %s (%s) | %.5f → %.5f",
        asset, trade_id, old_stop, new_stop,
    )


def log_kill_switch(loss_pct: float) -> None:
    ts = datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    _sys_log.critical(
        "[%s] DAILY KILL SWITCH TRIGGERED — loss %.2f%% hit 10%% limit",
        ts, loss_pct,
    )


def log_error(component: str, message: str, exc: Optional[Exception] = None) -> None:
    logging.getLogger(component).error(message, exc_info=exc)
