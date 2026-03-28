"""Unified market data client.

Abstracts three data sources:
  • CCXT (Binance public API) — crypto OHLCV
  • Finnhub                   — forex, commodities, stocks (intraday)
  • yfinance                  — stocks, ETFs, indices (historical fallback)

All functions return a standardised DataFrame with columns:
    time   (UTC datetime, timezone-aware)
    open   high   low   close   volume

Usage:
    from src.data.market_data import get_historical_ohlcv, get_latest_candles
"""

from __future__ import annotations


import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import requests


# Import Kraken WebSocket client
try:
    from src.data.kraken_ws_client import KrakenWebSocketClient
    _KRAKEN_WS_AVAILABLE = True
except ImportError:
    _KRAKEN_WS_AVAILABLE = False
    KrakenWebSocketClient = None

# Import Coinbase WebSocket client
try:
    from src.data.coinbase_ws_client import CoinbaseWebSocketClient
    _COINBASE_WS_AVAILABLE = True
except ImportError:
    _COINBASE_WS_AVAILABLE = False
    CoinbaseWebSocketClient = None

log = logging.getLogger(__name__)

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False
    log.warning("ccxt not installed — crypto data unavailable")

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    log.warning("yfinance not installed — yfinance source unavailable")

try:
    from src.config import FINNHUB_API_KEY, BINANCE_API_KEY, BINANCE_SECRET
except ImportError:
    FINNHUB_API_KEY  = ""
    BINANCE_API_KEY  = ""
    BINANCE_SECRET   = ""

try:
    from src.data.symbol_mapper import to_ccxt, to_yfinance, to_finnhub
except ImportError:
    def to_ccxt(s):     return None
    def to_yfinance(s): return None
    def to_finnhub(s):  return None


def _resolve_symbol(symbol: str, source: str) -> str:
    """Translate a canonical symbol to the source-specific format.

    Falls back to the original symbol if no mapping exists.
    """
    if source == "ccxt":
        return to_ccxt(symbol) or symbol
    if source == "finnhub":
        return to_finnhub(symbol) or symbol
    if source == "yfinance":
        return to_yfinance(symbol) or symbol
    return symbol

# Timeframe string mapping
TIMEFRAME_CCXT: dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "2h": "2h", "4h": "4h",
    "6h": "6h", "8h": "8h", "12h": "12h", "1d": "1d",
    "3d": "3d", "1w": "1w",
}

TIMEFRAME_FINNHUB: dict[str, str] = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "1d": "D", "1w": "W",
}

TIMEFRAME_YFINANCE: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1d": "1d", "1w": "1wk",
}

# ── Internal helpers ──────────────────────────────────────────────────────────

def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure standard OHLCV column names and UTC time index."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for alias in ("timestamp", "date", "datetime"):
        if alias in df.columns:
            df = df.rename(columns={alias: "time"})
    if "time" not in df.columns and isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={"index": "time", "Datetime": "time", "Date": "time"})
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["time", "open", "high", "low", "close", "volume"]].dropna(
        subset=["open", "high", "low", "close"]
    )
    df = df.sort_values("time").reset_index(drop=True)
    return df


# ── CCXT (crypto) ─────────────────────────────────────────────────────────────

def _get_ccxt_exchange(resolved_pair: str):
    """Pick exchange by pair. Bitstamp for BTC/USD (TradingView Bitstamp parity)."""
    if not _CCXT_AVAILABLE:
        raise RuntimeError("ccxt not installed")
    if resolved_pair == "BTC/USD":
        return ccxt.bitstamp({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
    # Binance — READ-ONLY public OHLCV for most crypto pairs
    return ccxt.binance({
        "apiKey":  BINANCE_API_KEY  or None,
        "secret":  BINANCE_SECRET   or None,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })


def _fetch_ccxt(
    symbol:    str,
    timeframe: str,
    since_ms:  Optional[int] = None,
    limit:     int = 500,
) -> pd.DataFrame:
    exc  = _get_ccxt_exchange(symbol)
    tf   = TIMEFRAME_CCXT.get(timeframe, "1h")
    bars = exc.fetch_ohlcv(symbol, tf, since=since_ms, limit=limit)
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return _normalise(df)


# ── Finnhub (forex / stocks / commodities) ────────────────────────────────────

_FINNHUB_BASE = "https://finnhub.io/api/v1"


def _fetch_finnhub(
    symbol:    str,
    timeframe: str,
    from_ts:   int,
    to_ts:     int,
) -> pd.DataFrame:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY not set in .env")
    resolution = TIMEFRAME_FINNHUB.get(timeframe, "60")
    params = {
        "symbol":     symbol,
        "resolution": resolution,
        "from":       from_ts,
        "to":         to_ts,
        "token":      FINNHUB_API_KEY,
    }
    resp = requests.get(f"{_FINNHUB_BASE}/forex/candle", params=params, timeout=15)
    if resp.status_code != 200:
        # Try stock endpoint
        resp = requests.get(f"{_FINNHUB_BASE}/stock/candle", params=params, timeout=15)
    data = resp.json()
    if data.get("s") != "ok" or not data.get("t"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "time":   pd.to_datetime(data["t"], unit="s", utc=True),
        "open":   data["o"],
        "high":   data["h"],
        "low":    data["l"],
        "close":  data["c"],
        "volume": data.get("v", [0] * len(data["t"])),
    })
    return _normalise(df)


# ── yfinance (stocks / ETFs / historical) ─────────────────────────────────────

def _fetch_yfinance(
    symbol:    str,
    timeframe: str,
    start:     datetime,
    end:       datetime,
) -> pd.DataFrame:
    if not _YF_AVAILABLE:
        raise RuntimeError("yfinance not installed")
    interval = TIMEFRAME_YFINANCE.get(timeframe, "1h")
    ticker   = yf.Ticker(symbol)
    df       = ticker.history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval=interval,
        auto_adjust=True,
    )
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    date_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={date_col: "time", "Open": "open", "High": "high",
                             "Low": "low", "Close": "close", "Volume": "volume"})
    return _normalise(df)


# ── Public API ────────────────────────────────────────────────────────────────

def get_historical_ohlcv(
    symbol:    str,
    timeframe: str,
    start:     datetime,
    end:       Optional[datetime] = None,
    source:    Optional[str]      = None,
) -> pd.DataFrame:
    """Fetch standardised OHLCV data for any asset.

    Args:
        symbol:    normalised symbol string  e.g. "BTC/USDT", "EUR/USD", "AAPL"
        timeframe: timeframe string          e.g. "1h", "4h", "1d"
        start:     inclusive start datetime
        end:       exclusive end datetime    (defaults to now)
        source:    force a specific source   "ccxt" | "finnhub" | "yfinance"

    Returns:
        DataFrame with columns: time, open, high, low, close, volume
    """
    if end is None:
        end = datetime.now(timezone.utc)
    start_utc = _to_utc(start)
    end_utc   = _to_utc(end)

    errors: list[str] = []

    # Auto-detect source if not forced
    is_crypto = "/" in symbol and any(
        c in symbol.upper() for c in ["BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "USDT"]
    )
    is_forex = "/" in symbol and not is_crypto

    if source is None:
        if is_crypto:
            source = "ccxt"
        elif is_forex:
            source = "finnhub"
        else:
            source = "yfinance"


    if source == "ccxt":
        try:
            resolved = _resolve_symbol(symbol, "ccxt")
            since_ms = int(start_utc.timestamp() * 1000)
            # Paginate for large ranges
            all_dfs = []
            limit   = 1000
            while True:
                df = _fetch_ccxt(resolved, timeframe, since_ms=since_ms, limit=limit)
                if df.empty:
                    break
                all_dfs.append(df)
                if len(df) < limit:
                    break
                since_ms = int(df["time"].iloc[-1].timestamp() * 1000) + 1
                time.sleep(0.1)  # respect rate limit
            if all_dfs:
                result = pd.concat(all_dfs).drop_duplicates("time").sort_values("time")
                return result[result["time"] <= end_utc].reset_index(drop=True)
        except Exception as e:
            errors.append(f"ccxt: {e}")

    if source == "finnhub":
        try:
            resolved = _resolve_symbol(symbol, "finnhub")
            df = _fetch_finnhub(
                resolved,
                timeframe,
                int(start_utc.timestamp()),
                int(end_utc.timestamp()),
            )
            if not df.empty:
                return df
        except Exception as e:
            errors.append(f"finnhub: {e}")
        # Fallback to yfinance if finnhub failed, but only for non-crypto assets
        if not is_crypto:
            try:
                resolved_yf = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved_yf, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("Finnhub failed for %s, used yfinance fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"yfinance-fallback: {e}")

    if source in ("yfinance", "fallback"):
        # Only allow yfinance for non-crypto assets
        if not is_crypto:
            try:
                resolved = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved, timeframe, start_utc, end_utc)
                if not df.empty:
                    return df
            except Exception as e:
                errors.append(f"yfinance: {e}")

    log.error("get_historical_ohlcv failed for %s %s: %s", symbol, timeframe, "; ".join(errors))
    return pd.DataFrame()



def get_latest_candles(
    symbol:    str,
    timeframe: str,
    count:     int = 200,
    source:    Optional[str] = None,
) -> pd.DataFrame:
    """Fetch the most recent N candles for a symbol.

    Uses Kraken WebSocket for crypto (if available) for 5m, 15m, 30m, 1h, 4h, else falls back to get_historical_ohlcv.
    """
    tf_mins: dict[str, int] = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080,
    }
    mins_per_bar  = tf_mins.get(timeframe, 60)
    look_back_min = mins_per_bar * count * 2
    start = datetime.now(timezone.utc) - timedelta(minutes=look_back_min)

    # Use Coinbase WebSocket for BTC/USD and ETH/USD ticker (reliable price reference)
    is_crypto = "/" in symbol and any(c in symbol.upper() for c in ["BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "USDT"])
    coinbase_products = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}
    if _COINBASE_WS_AVAILABLE and is_crypto and symbol in coinbase_products:
        try:
            client = CoinbaseWebSocketClient()
            import asyncio
            async def fetch_cb():
                await client.connect()
                await client.subscribe_ticker([coinbase_products[symbol]])
                await asyncio.wait_for(client.listen(), timeout=5)
            try:
                asyncio.run(fetch_cb())
            except Exception:
                pass
            df = client.get_ticker_df(coinbase_products[symbol])
            if not df.empty:
                # Convert ticker to OHLCV-like DataFrame
                df = df.rename(columns={"price": "close"})
                df["open"] = df["close"]
                df["high"] = df["close"]
                df["low"] = df["close"]
                df["volume"] = df["volume_24h"] if "volume_24h" in df else 0.0
                df = df[["time", "open", "high", "low", "close", "volume"]]
                return df.tail(count).reset_index(drop=True)
        except Exception as e:
            log.warning(f"Coinbase WS failed: {e}")

    # Fallback to Kraken WebSocket for crypto and supported timeframes
    supported_kraken_tfs = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
    if (
        _KRAKEN_WS_AVAILABLE and is_crypto and timeframe in supported_kraken_tfs
    ):
        try:
            client = KrakenWebSocketClient()
            import asyncio
            async def fetch_ws():
                await client.connect()
                await client.subscribe_ohlc(symbol, interval=supported_kraken_tfs[timeframe])
                await asyncio.wait_for(client.listen(), timeout=5)
            try:
                asyncio.run(fetch_ws())
            except Exception:
                pass
            df = client.get_ohlc_df(symbol)
            if not df.empty:
                return df.tail(count).reset_index(drop=True)
        except Exception as e:
            log.warning(f"Kraken WS failed: {e}")

    # Fallback to historical fetch
    df = get_historical_ohlcv(symbol, timeframe, start=start, source=source)
    if df.empty:
        return df
    return df.tail(count).reset_index(drop=True)
