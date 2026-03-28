"""Pushover push notification sender.

Fires phone alerts on:
  • New BUY signal detected
  • New SELL signal detected
  • Trade opened (order placed)
  • Trade closed (with PnL)
  • Daily kill switch triggered
  • System errors / connectivity failures

Requires PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY in .env
One-time app cost: ~$5 at pushover.net
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

try:
    from src.config import PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY
except ImportError:
    PUSHOVER_APP_TOKEN = ""
    PUSHOVER_USER_KEY  = ""

log = logging.getLogger(__name__)

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Priority levels
PRIORITY_SILENT  = -2
PRIORITY_QUIET   =  -1
PRIORITY_NORMAL  =  0
PRIORITY_HIGH    =  1
PRIORITY_CONFIRM =  2   # requires callback — avoid for automated messages


def _send(
    message:  str,
    title:    str  = "AlgoBot",
    priority: int  = PRIORITY_NORMAL,
    sound:    str  = "pushover",
) -> bool:
    """Send one Pushover notification. Returns True on success."""
    if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
        log.debug("Pushover not configured — skipping notification: %s", title)
        return False

    try:
        resp = requests.post(
            _PUSHOVER_URL,
            data={
                "token":    PUSHOVER_APP_TOKEN,
                "user":     PUSHOVER_USER_KEY,
                "message":  message,
                "title":    title,
                "priority": priority,
                "sound":    sound,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.warning("Pushover returned %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        log.error("Pushover send failed: %s", exc)
    return False


# ── Typed notification helpers ─────────────────────────────────────────────────

def notify_buy_signal(
    asset:          str,
    timeframe:      str,
    entry_price:    float,
    stop_loss:      float,
    profit_est_pct: float,
    ml_confidence:  Optional[float],
    timestamp_str:  str,
) -> bool:
    conf_str = f"{ml_confidence*100:.0f}%" if ml_confidence is not None else "N/A"
    message = (
        f"📈 BUY SIGNAL\n"
        f"Asset: {asset}  TF: {timeframe}\n"
        f"Entry: {entry_price:.5f}\n"
        f"Stop Loss: {stop_loss:.5f} (2%)\n"
        f"Est. Move: {profit_est_pct:.2f}%\n"
        f"ML Conf: {conf_str}\n"
        f"Time: {timestamp_str}"
    )
    return _send(message, title=f"BUY — {asset}", priority=PRIORITY_HIGH, sound="cashregister")


def notify_sell_signal(
    asset:          str,
    timeframe:      str,
    entry_price:    float,
    stop_loss:      float,
    profit_est_pct: float,
    ml_confidence:  Optional[float],
    timestamp_str:  str,
) -> bool:
    conf_str = f"{ml_confidence*100:.0f}%" if ml_confidence is not None else "N/A"
    message = (
        f"📉 SELL SIGNAL\n"
        f"Asset: {asset}  TF: {timeframe}\n"
        f"Entry: {entry_price:.5f}\n"
        f"Stop Loss: {stop_loss:.5f} (2%)\n"
        f"Est. Move: {profit_est_pct:.2f}%\n"
        f"ML Conf: {conf_str}\n"
        f"Time: {timestamp_str}"
    )
    return _send(message, title=f"SELL — {asset}", priority=PRIORITY_HIGH, sound="cashregister")


def notify_order_placed(
    trade_id:     str,
    signal_type:  str,
    asset:        str,
    entry_price:  float,
    stop_loss:    float,
    position_size:float,
    timestamp_str:str,
) -> bool:
    message = (
        f"✅ ORDER PLACED\n"
        f"ID: {trade_id[:8]}\n"
        f"Type: {signal_type}  Asset: {asset}\n"
        f"Entry: {entry_price:.5f}  Size: {position_size:.4f}\n"
        f"Hard SL: {stop_loss:.5f}\n"
        f"Time: {timestamp_str}"
    )
    return _send(message, title=f"ORDER — {asset}", priority=PRIORITY_NORMAL)


def notify_trade_closed(
    trade_id:     str,
    asset:        str,
    signal_type:  str,
    pnl:          float,
    pnl_pct:      float,
    close_reason: str,
    timestamp_str:str,
) -> bool:
    emoji = "🟢" if pnl >= 0 else "🔴"
    message = (
        f"{emoji} TRADE CLOSED\n"
        f"ID: {trade_id[:8]}\n"
        f"Type: {signal_type}  Asset: {asset}\n"
        f"Reason: {close_reason}\n"
        f"PnL: {pnl:+.2f} ({pnl_pct:+.2f}%)\n"
        f"Time: {timestamp_str}"
    )
    priority = PRIORITY_NORMAL if pnl >= 0 else PRIORITY_HIGH
    return _send(message, title=f"CLOSED — {asset}", priority=priority)


def notify_kill_switch(loss_pct: float, timestamp_str: str) -> bool:
    message = (
        f"🚨 DAILY KILL SWITCH TRIGGERED\n"
        f"Loss: {loss_pct:.2f}% hit the 10% daily limit.\n"
        f"All trading halted for the rest of the day.\n"
        f"Time: {timestamp_str}"
    )
    return _send(message, title="KILL SWITCH", priority=PRIORITY_HIGH, sound="siren")


def notify_error(component: str, message: str) -> bool:
    msg = f"⚠️ ERROR in {component}\n{message}"
    return _send(msg, title="AlgoBot Error", priority=PRIORITY_HIGH)
