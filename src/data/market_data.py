"""Unified market data client.

Abstracts four data sources:
  • Polygon.io (api.massive.com)         — stocks, crypto, forex OHLCV (broad coverage)
  • CCXT (Binance/Bitstamp public API)   — crypto OHLCV (primary for crypto)
  • Finnhub                              — forex, commodities, stocks (intraday)
  • yfinance                             — fallback for all non-crypto assets

  OpenBB SDK is imported opportunistically but is currently non-functional
  (openbb 4.7.1 / openbb-core 1.6.7 version mismatch: OBBject_EquityInfo is
  not injected into openbb_core.app.provider_interface at runtime). When
  "openbb" is requested as source, yfinance is used instead.

Source priority by asset class
-------------------------------
  Crypto:      ccxt  → polygon  → yfinance
  Forex:       finnhub  → polygon  → yfinance
  Stocks/ETFs: polygon  → yfinance

All functions return a standardised DataFrame with columns:
    time   (UTC datetime, timezone-aware)
    open   high   low   close   volume

Usage:
    from src.data.market_data import get_historical_ohlcv, get_latest_candles
"""

from __future__ import annotations


import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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

# Import IBKR Market Data Client
try:
    from src.data.ibkr.client import IBKRMarketDataClient
    _IBKR_AVAILABLE = True
except ImportError:
    _IBKR_AVAILABLE = False
    IBKRMarketDataClient = None

log = logging.getLogger(__name__)

# Global IBKR client instance (initialized on first use)
_ibkr_client: Optional[Any] = None
_ibkr_init_lock = threading.Lock()

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
    from openbb import obb as _obb_raw  # type: ignore[attr-defined]
    # Trigger lazy import of the equity extension to verify it's actually usable.
    # openbb 4.7.1 has a bug where OBBject_EquityInfo is not injected into
    # openbb_core.app.provider_interface, causing a deferred ImportError.
    _type_check = type(_obb_raw.equity)  # noqa: F841  # type: ignore[attr-defined]
    _OPENBB_AVAILABLE = True
    _obb: Any = _obb_raw
except Exception:
    _OPENBB_AVAILABLE = False
    _obb: Any = None

try:
    from src.config import FINNHUB_API_KEY, BINANCE_API_KEY, BINANCE_SECRET, POLYGON_API_KEY
except ImportError:
    FINNHUB_API_KEY  = ""
    BINANCE_API_KEY  = ""
    BINANCE_SECRET   = ""
    POLYGON_API_KEY  = ""

try:
    from src.data.symbol_mapper import to_ccxt, to_yfinance, to_finnhub
except ImportError:
    def to_ccxt(symbol: str) -> str | None:     return None  # type: ignore[misc]
    def to_yfinance(symbol: str) -> str | None: return None  # type: ignore[misc]
    def to_finnhub(symbol: str) -> str | None:  return None  # type: ignore[misc]


def _resolve_symbol(symbol: str, source: str) -> str:
    """Translate a canonical symbol to the source-specific format.

    Falls back to the original symbol if no mapping exists.
    """
    if source == "ccxt":
        return to_ccxt(symbol) or symbol
    if source == "finnhub":
        return to_finnhub(symbol) or symbol
    if source in ("yfinance", "openbb"):
        return to_yfinance(symbol) or symbol
    if source == "polygon":
        return _to_polygon_ticker(symbol)
    if source == "ibkr":
        # IBKR uses canonical symbols directly
        return symbol
    return symbol


def _to_polygon_ticker(symbol: str) -> str:
    """Convert a canonical symbol to the Polygon.io ticker format.

    Polygon uses:
      Stocks  → plain uppercase ticker:  AAPL, TSLA
      Crypto  → X:BTCUSD               (no slash, X: prefix)
      Forex   → C:EURUSD               (no slash, C: prefix)
    """
    upper = symbol.upper().replace("/", "")
    # Detect crypto by common quote/base patterns
    crypto_keywords = {"BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "USDT", "DOGE", "AVAX",
                       "MATIC", "DOT", "LINK", "LTC", "UNI", "ATOM"}
    is_crypto = any(k in upper for k in crypto_keywords)
    # Forex: 6-char all-alpha pairs like EURUSD, GBPUSD
    is_forex = (
        not is_crypto
        and len(upper) == 6
        and upper.isalpha()
        and upper[:3] in {"EUR", "GBP", "USD", "JPY", "AUD", "NZD", "CAD", "CHF"}
    )
    if is_crypto:
        return f"X:{upper}"
    if is_forex:
        return f"C:{upper}"
    return upper


def _get_ibkr_client() -> Optional[Any]:
    """Get or initialize the global IBKR client instance.
    
    Returns None if IBKR is not available or connection fails.
    """
    global _ibkr_client
    
    if not _IBKR_AVAILABLE:
        return None
    
    with _ibkr_init_lock:
        if _ibkr_client is None:
            try:
                log.info("Initializing IBKR Market Data Client...")
                _ibkr_client = IBKRMarketDataClient()  # type: ignore[operator]
                
                # Connect asynchronously
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # No event loop running, create one
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    connected = loop.run_until_complete(_ibkr_client.connect())  # type: ignore[union-attr]
                    if not connected:
                        log.error("Failed to connect to IBKR")
                        _ibkr_client = None
                        return None
                else:
                    # Event loop already running, schedule connection
                    asyncio.create_task(_ibkr_client.connect())  # type: ignore[union-attr]
                
                log.info("IBKR Market Data Client initialized successfully")
            except Exception as e:
                log.error(f"Failed to initialize IBKR client: {e}")
                _ibkr_client = None
                return None
        
        return _ibkr_client

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
    "1m": "1m", "3m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1d": "1d", "1w": "1wk",
}
# Timeframes that need resampling after yfinance fetch (fetch_interval -> target_interval)
_YF_RESAMPLE: dict[str, str] = {
    "3m": "3min",
}

# OpenBB uses yfinance-compatible interval strings; unsupported ones map to nearest.
TIMEFRAME_OPENBB: dict[str, str] = {
    "1m": "1m",  "3m": "5m",  "5m": "5m",  "15m": "15m",
    "30m": "30m", "1h": "1h",  "2h": "1h",  "4h": "1h",
    "1d": "1d",  "1w": "1wk",
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
        return ccxt.bitstamp({  # type: ignore[possibly-unbound]
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
    # Binance — READ-ONLY public OHLCV for most crypto pairs
    return ccxt.binance({  # type: ignore[possibly-unbound]
        "apiKey":  BINANCE_API_KEY  or "",
        "secret":  BINANCE_SECRET   or "",
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
    ticker   = yf.Ticker(symbol)  # type: ignore[possibly-unbound]
    # For intraday intervals, start and end may fall on the same calendar day;
    # yfinance returns empty when start_date == end_date, so advance end by 1 day.
    intraday_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}
    end_dt = end + timedelta(days=1) if interval in intraday_intervals else end
    df       = ticker.history(
        start=start.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        interval=interval,
        auto_adjust=True,
    )
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    date_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={date_col: "time", "Open": "open", "High": "high",
                             "Low": "low", "Close": "close", "Volume": "volume"})
    df = _normalise(df)
    # Resample to target timeframe when yfinance doesn't support it natively (e.g. 3m)
    resample_rule = _YF_RESAMPLE.get(timeframe)
    if resample_rule and not df.empty and "time" in df.columns:
        try:
            df = df.set_index("time")
            df = df.resample(resample_rule).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna(subset=["open", "close"]).reset_index()
        except Exception:
            pass
    return df


# ── OpenBB (equity / crypto / forex via yfinance provider) ───────────────────

def _fetch_openbb(
    symbol:    str,
    timeframe: str,
    start:     datetime,
    end:       datetime,
    is_crypto: bool = False,
    is_forex:  bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV via OpenBB SDK (yfinance provider).

    Routes to the correct OpenBB namespace based on asset class:
      Equities/ETFs  -> obb.equity.price.historical
      Crypto         -> obb.crypto.price.historical
      Forex/FX       -> obb.currency.price.historical
    """
    if not _OPENBB_AVAILABLE or _obb is None:
        raise RuntimeError("openbb not installed")
    interval   = TIMEFRAME_OPENBB.get(timeframe, "1h")
    start_date = start.strftime("%Y-%m-%d")
    end_date   = end.strftime("%Y-%m-%d")
    if is_crypto:
        result = _obb.crypto.price.historical(
            symbol, start_date=start_date, end_date=end_date,
            interval=interval, provider="yfinance",
        )
    elif is_forex:
        result = _obb.currency.price.historical(
            symbol, start_date=start_date, end_date=end_date,
            interval=interval, provider="yfinance",
        )
    else:
        result = _obb.equity.price.historical(
            symbol, start_date=start_date, end_date=end_date,
            interval=interval, provider="yfinance",
        )
    df = result.to_df().reset_index()
    return _normalise(df)


# ── Polygon.io (stocks / crypto / forex) ─────────────────────────────────────

_POLYGON_BASE = "https://api.massive.com/v2"

# Polygon uses (multiplier, timespan) pairs.
# timeframe -> (multiplier, timespan)
_POLYGON_TIMEFRAME: dict[str, tuple[int, str]] = {
    "1m":  (1,  "minute"),
    "3m":  (3,  "minute"),
    "5m":  (5,  "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h":  (1,  "hour"),
    "2h":  (2,  "hour"),
    "4h":  (4,  "hour"),
    "1d":  (1,  "day"),
    "1w":  (1,  "week"),
}


def _fetch_polygon(
    ticker:    str,
    timeframe: str,
    start:     datetime,
    end:       datetime,
) -> pd.DataFrame:
    """Fetch OHLCV bars from Polygon.io REST API.

    Handles pagination automatically via next_url.
    Works for stocks (AAPL), crypto (X:BTCUSD), and forex (C:EURUSD).
    """
    if not POLYGON_API_KEY:
        raise RuntimeError("POLYGON_API_KEY not set in .env")
    multiplier, timespan = _POLYGON_TIMEFRAME.get(timeframe, (1, "hour"))
    from_str = start.strftime("%Y-%m-%d")
    to_str   = end.strftime("%Y-%m-%d")
    url = (
        f"{_POLYGON_BASE}/aggs/ticker/{ticker}/range/{multiplier}/{timespan}"
        f"/{from_str}/{to_str}"
    )
    params: dict = {
        "adjusted": "true",
        "sort":     "asc",
        "limit":    50000,
        "apiKey":   POLYGON_API_KEY,
    }
    all_results: list[dict] = []
    while url:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("OK", "DELAYED"):
            break
        all_results.extend(data.get("results") or [])
        # Follow pagination cursor; next_url already has apiKey embedded
        url    = data.get("next_url")
        params = {}  # don't re-send params with next_url
    if not all_results:
        return pd.DataFrame()
    df = pd.DataFrame(all_results)
    df = df.rename(columns={"t": "time", "o": "open", "h": "high",
                             "l": "low",  "c": "close", "v": "volume"})
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
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
        source:    force a specific source   "polygon" | "ccxt" | "finnhub" | "yfinance" | "openbb" | "ibkr"

    Returns:
        DataFrame with columns: time, open, high, low, close, volume
    """
    if end is None:
        end = datetime.now(timezone.utc)
    start_utc = _to_utc(start)
    end_utc   = _to_utc(end)

    errors: list[str] = []

    # Auto-detect source: prefer catalogue asset class over keyword heuristic
    try:
        from src.data.symbol_mapper import get_asset_class as _get_asset_class
        _asset_cls = _get_asset_class(symbol)
        is_crypto = _asset_cls == "crypto"
        is_forex  = _asset_cls == "forex"
    except Exception:
        is_crypto = "/" in symbol and any(
            c in symbol.upper() for c in ["BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "USDT",
                                           "INJ", "AVAX", "LINK", "DOGE", "ARB", "APT"]
        )
        is_forex = "/" in symbol and not is_crypto

    if source is None:
        if is_crypto:
            source = "ccxt"
        elif is_forex:
            source = "finnhub"
        else:
            source = "polygon"  # primary for stocks/ETFs (Polygon.io)

    # Try IBKR if explicitly requested
    if source == "ibkr":
        try:
            client = _get_ibkr_client()
            if client:
                resolved = _resolve_symbol(symbol, "ibkr")
                
                # Run async fetch in event loop
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # No event loop running, create one
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    df = loop.run_until_complete(
                        client.get_historical_ohlcv(resolved, timeframe, start_utc, end_utc)
                    )
                else:
                    # Event loop already running, use it
                    df = asyncio.create_task(
                        client.get_historical_ohlcv(resolved, timeframe, start_utc, end_utc)
                    )
                    # Wait for result (blocking)
                    # df is a Task — get result by running the event loop
                    loop2 = asyncio.new_event_loop()
                    try:
                        df = loop2.run_until_complete(df)
                    finally:
                        loop2.close()

                if not df.empty:
                    return df
            else:
                errors.append("ibkr: client not available")
        except Exception as e:
            errors.append(f"ibkr: {e}")

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
        # Polygon fallback for crypto (X:BTCUSD style ticker)
        if is_crypto:
            try:
                pg_ticker = _to_polygon_ticker(symbol)
                df = _fetch_polygon(pg_ticker, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("CCXT failed for %s, used Polygon fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"polygon-crypto-fallback: {e}")
        # Final crypto fallback: raw yfinance
        if is_crypto:
            try:
                resolved_yf = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved_yf, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("CCXT+Polygon failed for %s, used yfinance fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"yfinance-crypto-fallback: {e}")

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
        # Polygon fallback for forex (C:EURUSD style)
        if not is_crypto:
            try:
                pg_ticker = _to_polygon_ticker(symbol)
                df = _fetch_polygon(pg_ticker, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("Finnhub failed for %s, used Polygon fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"polygon-forex-fallback: {e}")
        # Final forex fallback: raw yfinance
        if not is_crypto:
            try:
                resolved_yf = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved_yf, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("Finnhub+Polygon failed for %s, used yfinance fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"yfinance-fallback: {e}")

    if source == "polygon":
        # Primary for stocks/ETFs; also callable explicitly for crypto/forex
        try:
            pg_ticker = _resolve_symbol(symbol, "polygon")
            df = _fetch_polygon(pg_ticker, timeframe, start_utc, end_utc)
            if not df.empty:
                return df
        except Exception as e:
            errors.append(f"polygon: {e}")
        # yfinance safety net
        if not is_crypto:
            try:
                resolved = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved, timeframe, start_utc, end_utc)
                if not df.empty:
                    log.info("Polygon failed for %s, used yfinance fallback", symbol)
                    return df
            except Exception as e:
                errors.append(f"yfinance-polygon-fallback: {e}")

    if source == "openbb":
        # OpenBB is currently non-functional (openbb 4.7.1 / openbb-core 1.6.7
        # version mismatch: OBBject_EquityInfo not in provider_interface namespace).
        # Fall through to yfinance.
        if not is_crypto:
            try:
                resolved = _resolve_symbol(symbol, "yfinance")
                df = _fetch_yfinance(resolved, timeframe, start_utc, end_utc)
                if not df.empty:
                    return df
            except Exception as e:
                errors.append(f"openbb-yfinance-fallback: {e}")

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
    Supports IBKR as a data source when source="ibkr" is specified.
    """
    # Try IBKR if explicitly requested
    if source == "ibkr":
        try:
            client = _get_ibkr_client()
            if client:
                resolved = _resolve_symbol(symbol, "ibkr")
                df = client.get_latest_candles(resolved, timeframe, count)
                if not df.empty:
                    return df
            else:
                log.warning(f"IBKR client not available for {symbol}")
        except Exception as e:
            log.warning(f"IBKR fetch failed for {symbol}: {e}")
    
    tf_mins: dict[str, int] = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080,
    }
    mins_per_bar  = tf_mins.get(timeframe, 60)
    look_back_min = mins_per_bar * count * 2
    start = datetime.now(timezone.utc) - timedelta(minutes=look_back_min)

    # Determine if crypto symbol — use catalogue asset class for accuracy
    try:
        from src.data.symbol_mapper import get_asset_class as _get_ac
        is_crypto = _get_ac(symbol) == "crypto"
    except Exception:
        is_crypto = "/" in symbol and any(
            c in symbol.upper() for c in ["BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "USDT",
                                           "INJ", "AVAX", "LINK", "DOGE", "ARB", "APT"]
        )

    # Use Kraken WebSocket for crypto and supported timeframes
    supported_kraken_tfs = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
    if (
        _KRAKEN_WS_AVAILABLE and is_crypto and timeframe in supported_kraken_tfs
    ):
        try:
            client = KrakenWebSocketClient()  # type: ignore[operator]
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


def shutdown_ibkr_client():
    """Gracefully shutdown the IBKR client.
    
    Should be called on application shutdown to properly disconnect
    and persist cache.
    """
    global _ibkr_client
    
    if _ibkr_client is not None:
        try:
            log.info("Shutting down IBKR Market Data Client...")
            
            # Run async disconnect
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No event loop running, create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_ibkr_client.disconnect())
            else:
                # Event loop already running
                asyncio.create_task(_ibkr_client.disconnect())
            
            _ibkr_client = None
            log.info("IBKR Market Data Client shutdown complete")
        except Exception as e:
            log.error(f"Error shutting down IBKR client: {e}")
