"""Alpaca execution adapter (paper and live REST API).

Uses Alpaca Trading API v2 with `requests` (no extra SDK).
Maps bot canonical symbols to Alpaca: stocks as tickers, crypto as ``BASE/USD``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

from src.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
from src.data.symbol_mapper import canonical_symbol, get_asset_class

log = logging.getLogger(__name__)


def _client_order_id(trade_id: str) -> str:
    # Alpaca: max 48 chars, [A-Za-z0-9-_:]
    raw = re.sub(r"[^A-Za-z0-9_:\-]", "", trade_id)[:48]
    return raw or "ord"


class AlpacaAdapter:
    def __init__(self) -> None:
        base = (ALPACA_BASE_URL or "https://paper-api.alpaca.markets").rstrip("/")
        # Users sometimes paste endpoint ".../v2"; normalize to host root.
        if base.endswith("/v2"):
            base = base[:-3]
        self._base = base
        self._connected = False
        # trade_id -> execution state (in-memory only; restart loses mapping)
        self._trades: dict[str, dict[str, Any]] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }

    def _normalize_symbol(self, symbol: str) -> str:
        canon = canonical_symbol(symbol)
        ac = get_asset_class(canon)
        if ac == "crypto":
            if canon.endswith("USDT"):
                return f"{canon[:-4]}/USD"
            if canon.endswith("USD") and len(canon) > 3:
                base = canon[:-3]
                if base:
                    return f"{base}/USD"
            return canon.replace("USDT", "/USD") if "USDT" in canon else canon
        return canon

    def _format_qty(self, volume: float, asset_class: str) -> str:
        if asset_class == "crypto":
            s = f"{float(volume):.8f}".rstrip("0").rstrip(".")
            return s if s else "0"
        # stocks / fractional shares
        s = f"{float(volume):.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    def connect(self) -> bool:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            log.warning("Alpaca credentials missing — Alpaca disabled")
            self._connected = False
            return False
        try:
            r = requests.get(
                f"{self._base}/v2/account",
                headers=self._headers(),
                timeout=15,
            )
            self._connected = r.status_code == 200
            if not self._connected:
                log.warning("Alpaca connect failed (%s): %s", r.status_code, r.text[:300])
            return self._connected
        except Exception as e:
            log.warning("Alpaca connect error: %s", e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return bool(self._connected)

    def _wait_order_final(self, order_id: str, timeout_s: float = 45.0) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = requests.get(
                f"{self._base}/v2/orders/{order_id}",
                headers=self._headers(),
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            st = (data.get("status") or "").lower()
            if st in ("filled", "canceled", "expired", "rejected", "done_for_day"):
                return data
            time.sleep(0.4)
        return {}

    def place_order(
        self,
        signal_type: str,
        symbol: str,
        volume: float,
        expected_entry: float,
        stop_loss: float,
        trade_id: str,
        take_profit: Optional[float] = None,
    ) -> Optional[dict]:
        if not self._connected and not self.connect():
            raise RuntimeError("AlpacaAdapter not connected")

        sym = self._normalize_symbol(symbol)
        ac = get_asset_class(canonical_symbol(symbol))
        side = "buy" if str(signal_type).upper() == "BUY" else "sell"
        qty_str = self._format_qty(volume, ac)
        tif = "gtc" if ac == "crypto" else "day"

        cid = _client_order_id(trade_id)
        body: dict[str, Any] = {
            "symbol": sym,
            "qty": qty_str,
            "side": side,
            "type": "market",
            "time_in_force": tif,
            "client_order_id": cid,
        }
        if ac == "crypto":
            body["time_in_force"] = "gtc"

        try:
            r = requests.post(
                f"{self._base}/v2/orders",
                headers=self._headers(),
                data=json.dumps(body),
                timeout=30,
            )
            if r.status_code not in (200, 201):
                log.error("Alpaca market order failed: %s %s", r.status_code, r.text[:400])
                return None
            order = r.json()
            oid = order.get("id")
            if not oid:
                return None

            filled = self._wait_order_final(str(oid))
            filled_px = float(filled.get("filled_avg_price") or order.get("filled_avg_price") or expected_entry)

            stop_side = "sell" if side == "buy" else "buy"
            stop_body = {
                "symbol": sym,
                "qty": qty_str,
                "side": stop_side,
                "type": "stop",
                "stop_price": str(round(float(stop_loss), 8)),
                "time_in_force": "gtc",
            }
            rs = requests.post(
                f"{self._base}/v2/orders",
                headers=self._headers(),
                data=json.dumps(stop_body),
                timeout=30,
            )
            stop_id = None
            if rs.status_code in (200, 201):
                stop_id = rs.json().get("id")
            else:
                log.warning("Alpaca stop order failed: %s %s", rs.status_code, rs.text[:300])

            tp_id = None
            if take_profit is not None:
                tp_side = stop_side
                tp_body = {
                    "symbol": sym,
                    "qty": qty_str,
                    "side": tp_side,
                    "type": "limit",
                    "limit_price": str(round(float(take_profit), 8)),
                    "time_in_force": "gtc",
                }
                rt = requests.post(
                    f"{self._base}/v2/orders",
                    headers=self._headers(),
                    data=json.dumps(tp_body),
                    timeout=30,
                )
                if rt.status_code in (200, 201):
                    tp_id = rt.json().get("id")

            self._trades[trade_id] = {
                "symbol": sym,
                "qty": qty_str,
                "side": side,
                "stop_order_id": str(stop_id) if stop_id else None,
                "tp_order_id": str(tp_id) if tp_id else None,
            }

            return {
                "order_id": str(oid),
                "symbol": sym,
                "side": side,
                "volume": float(volume),
                "price": filled_px,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "status": filled.get("status") or order.get("status"),
                "stop_order_id": stop_id,
                "tp_order_id": tp_id,
            }
        except Exception as e:
            log.error("Alpaca place_order error: %s", e)
            return None

    def _cancel_if(self, oid: Optional[str]) -> None:
        if not oid:
            return
        try:
            requests.delete(
                f"{self._base}/v2/orders/{oid}",
                headers=self._headers(),
                timeout=15,
            )
        except Exception as e:
            log.debug("Alpaca cancel order %s: %s", oid, e)

    def close_order(self, trade_id: str) -> bool:
        if not self._connected and not self.connect():
            return False
        st = self._trades.get(trade_id)
        if not st:
            log.warning("Alpaca: unknown trade_id=%s (restart cleared state?)", trade_id)
            return False
        sym = st["symbol"]
        self._cancel_if(st.get("stop_order_id"))
        self._cancel_if(st.get("tp_order_id"))
        try:
            enc = quote(sym, safe="")
            r = requests.delete(
                f"{self._base}/v2/positions/{enc}",
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code in (200, 204):
                del self._trades[trade_id]
                return True
            log.warning("Alpaca close position %s: %s %s", sym, r.status_code, r.text[:300])
        except Exception as e:
            log.error("Alpaca close_order: %s", e)
        return False

    def update_trailing_stop(self, trade_id: str, new_sl: float) -> bool:
        return self.modify_position_sltp(trade_id, new_sl, None)

    def modify_position_sltp(self, trade_id: str, new_sl: float, new_tp: Optional[float] = None) -> bool:
        if not self._connected and not self.connect():
            return False
        st = self._trades.get(trade_id)
        if not st:
            log.warning("Alpaca modify: unknown trade_id=%s", trade_id)
            return False
        sym = st["symbol"]
        qty_str = st["qty"]
        side: str = st["side"]
        stop_side = "sell" if side == "buy" else "buy"

        self._cancel_if(st.get("stop_order_id"))
        if new_tp is not None:
            self._cancel_if(st.get("tp_order_id"))

        ok = True
        try:
            rs = requests.post(
                f"{self._base}/v2/orders",
                headers=self._headers(),
                data=json.dumps(
                    {
                        "symbol": sym,
                        "qty": qty_str,
                        "side": stop_side,
                        "type": "stop",
                        "stop_price": str(round(float(new_sl), 8)),
                        "time_in_force": "gtc",
                    }
                ),
                timeout=30,
            )
            if rs.status_code in (200, 201):
                st["stop_order_id"] = str(rs.json().get("id"))
            else:
                ok = False
                log.warning("Alpaca new stop failed: %s %s", rs.status_code, rs.text[:300])

            if new_tp is not None:
                rt = requests.post(
                    f"{self._base}/v2/orders",
                    headers=self._headers(),
                    data=json.dumps(
                        {
                            "symbol": sym,
                            "qty": qty_str,
                            "side": stop_side,
                            "type": "limit",
                            "limit_price": str(round(float(new_tp), 8)),
                            "time_in_force": "gtc",
                        }
                    ),
                    timeout=30,
                )
                if rt.status_code in (200, 201):
                    st["tp_order_id"] = str(rt.json().get("id"))
                else:
                    ok = False
        except Exception as e:
            log.error("Alpaca modify_position_sltp: %s", e)
            return False
        return ok
