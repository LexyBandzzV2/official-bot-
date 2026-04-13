"""Symbol mapper — normalises ticker symbols across data sources.

Every symbol used in the bot is stored internally as a canonical key, e.g.
``BTCUSDT``, ``EURUSD``, ``XAUUSD``.  The mapper translates between the
canonical key and the format expected by each data source.
"""

from __future__ import annotations
from typing import Dict, List, Optional
import json
from pathlib import Path

# ── Asset-class catalogue ─────────────────────────────────────────────────────
# Each entry: canonical_symbol -> {class, ccxt_id, finnhub_id, yf_ticker, display_name}
ASSET_CATALOGUE: Dict[str, dict] = {
    # Forex majors
    "EURUSD":  {"class": "forex",      "finnhub": "OANDA:EUR_USD",  "yf": "EURUSD=X",  "display": "EUR/USD"},
    "GBPUSD":  {"class": "forex",      "finnhub": "OANDA:GBP_USD",  "yf": "GBPUSD=X",  "display": "GBP/USD"},
    "USDJPY":  {"class": "forex",      "finnhub": "OANDA:USD_JPY",  "yf": "JPY=X",      "display": "USD/JPY"},
    "USDCAD":  {"class": "forex",      "finnhub": "OANDA:USD_CAD",  "yf": "CAD=X",      "display": "USD/CAD"},
    "AUDUSD":  {"class": "forex",      "finnhub": "OANDA:AUD_USD",  "yf": "AUDUSD=X",  "display": "AUD/USD"},
    "NZDUSD":  {"class": "forex",      "finnhub": "OANDA:NZD_USD",  "yf": "NZDUSD=X",  "display": "NZD/USD"},
    "USDCHF":  {"class": "forex",      "finnhub": "OANDA:USD_CHF",  "yf": "CHF=X",      "display": "USD/CHF"},
    "EURGBP":  {"class": "forex",      "finnhub": "OANDA:EUR_GBP",  "yf": "EURGBP=X",  "display": "EUR/GBP"},

    # Crypto
    "BTCUSDT": {"class": "crypto",     "ccxt": "BTC/USDT",          "yf": "BTC-USD",    "display": "BTC/USDT"},
    # Bitstamp spot (closer to TradingView BTC/USD on Bitstamp than Binance USDT)
    "BTCUSD":  {"class": "crypto",     "ccxt": "BTC/USD",            "yf": "BTC-USD",    "display": "BTC/USD"},
    "ETHUSDT": {"class": "crypto",     "ccxt": "ETH/USDT",          "yf": "ETH-USD",    "display": "ETH/USDT"},
    "SOLUSDT": {"class": "crypto",     "ccxt": "SOL/USDT",          "yf": "SOL-USD",    "display": "SOL/USDT"},
    "BNBUSDT": {"class": "crypto",     "ccxt": "BNB/USDT",          "yf": "BNB-USD",    "display": "BNB/USDT"},
    "XRPUSDT": {"class": "crypto",     "ccxt": "XRP/USDT",          "yf": "XRP-USD",    "display": "XRP/USDT"},
    "ADAUSDT": {"class": "crypto",     "ccxt": "ADA/USDT",          "yf": "ADA-USD",    "display": "ADA/USDT"},
    "DOGEUSDT":{"class": "crypto",     "ccxt": "DOGE/USDT",         "yf": "DOGE-USD",   "display": "DOGE/USDT"},
    "AVAXUSDT":{"class": "crypto",     "ccxt": "AVAX/USDT",         "yf": "AVAX-USD",   "display": "AVAX/USDT"},
    # Core 20 crypto additions (slash-form symbols normalize to canonical keys below)
    "ETHUSD":  {"class": "crypto",     "ccxt": "ETH/USDT",          "yf": "ETH-USD",    "display": "ETH/USD"},
    "SOLUSD":  {"class": "crypto",     "ccxt": "SOL/USDT",          "yf": "SOL-USD",    "display": "SOL/USD"},
    "AVAXUSD": {"class": "crypto",     "ccxt": "AVAX/USDT",         "yf": "AVAX-USD",   "display": "AVAX/USD"},
    "LINKUSD": {"class": "crypto",     "ccxt": "LINK/USDT",         "yf": "LINK-USD",   "display": "LINK/USD"},
    "DOGEUSD": {"class": "crypto",     "ccxt": "DOGE/USDT",         "yf": "DOGE-USD",   "display": "DOGE/USD"},
    "BNBUSD":  {"class": "crypto",     "ccxt": "BNB/USDT",          "yf": "BNB-USD",    "display": "BNB/USD"},
    "INJUSD":  {"class": "crypto",     "ccxt": "INJ/USDT",          "yf": "INJ-USD",    "display": "INJ/USD"},
    "ARBUSD":  {"class": "crypto",     "ccxt": "ARB/USDT",          "yf": "ARB-USD",    "display": "ARB/USD"},
    "APTUSD":  {"class": "crypto",     "ccxt": "APT/USDT",          "yf": "APT-USD",    "display": "APT/USD"},

    # Commodities
    "XAUUSD":  {"class": "commodity",  "finnhub": "OANDA:XAU_USD",  "yf": "GC=F",       "display": "Gold"},
    "XAGUSD":  {"class": "commodity",  "finnhub": "OANDA:XAG_USD",  "yf": "SI=F",       "display": "Silver"},
    "USOIL":   {"class": "commodity",  "finnhub": "OANDA:WTICO_USD","yf": "CL=F",        "display": "Crude Oil (WTI)"},
    "UKOIL":   {"class": "commodity",  "finnhub": "OANDA:BCO_USD",  "yf": "BZ=F",       "display": "Brent Crude"},

    # Indices
    "US30":    {"class": "index",      "finnhub": "OANDA:US30_USD", "yf": "^DJI",       "display": "Dow Jones"},
    "US500":   {"class": "index",      "finnhub": "OANDA:SPX500_USD","yf": "^GSPC",     "display": "S&P 500"},
    "NAS100":  {"class": "index",      "finnhub": "OANDA:NAS100_USD","yf": "^NDX",      "display": "NASDAQ 100"},

    # Canadian stocks (TSX)
    "SHOPCA":  {"class": "stock",      "finnhub": "TSX:SHOP",       "yf": "SHOP.TO",    "display": "Shopify (TSX)"},
    "RYCCA":   {"class": "stock",      "finnhub": "TSX:RY",         "yf": "RY.TO",      "display": "Royal Bank"},
    "TDCA":    {"class": "stock",      "finnhub": "TSX:TD",         "yf": "TD.TO",      "display": "TD Bank"},

    # US stocks
    "AAPL":    {"class": "stock",      "finnhub": "AAPL",           "yf": "AAPL",       "display": "Apple"},
    "TSLA":    {"class": "stock",      "finnhub": "TSLA",           "yf": "TSLA",       "display": "Tesla"},
    "NVDA":    {"class": "stock",      "finnhub": "NVDA",           "yf": "NVDA",       "display": "NVIDIA"},
    "MSFT":    {"class": "stock",      "finnhub": "MSFT",           "yf": "MSFT",       "display": "Microsoft"},
    "AMD":     {"class": "stock",      "finnhub": "AMD",            "yf": "AMD",        "display": "AMD"},
    "META":    {"class": "stock",      "finnhub": "META",           "yf": "META",       "display": "Meta"},
    "AMZN":    {"class": "stock",      "finnhub": "AMZN",           "yf": "AMZN",       "display": "Amazon"},
    "NFLX":    {"class": "stock",      "finnhub": "NFLX",           "yf": "NFLX",       "display": "Netflix"},
    "SMCI":    {"class": "stock",      "finnhub": "SMCI",           "yf": "SMCI",       "display": "Super Micro Computer"},
    "AMN":     {"class": "stock",      "finnhub": "AMN",            "yf": "AMN",        "display": "AMN Healthcare"},
    "QQQ":     {"class": "stock",      "finnhub": "QQQ",            "yf": "QQQ",        "display": "Invesco QQQ Trust"},
    "SPY":     {"class": "stock",      "finnhub": "SPY",            "yf": "SPY",        "display": "SPDR S&P 500 ETF"},
    "TQQQ":    {"class": "stock",      "finnhub": "TQQQ",           "yf": "TQQQ",       "display": "ProShares UltraPro QQQ"},
    "SOXL":    {"class": "stock",      "finnhub": "SOXL",           "yf": "SOXL",       "display": "Direxion Daily Semiconductor Bull 3X"},
    "TECL":    {"class": "stock",      "finnhub": "TECL",           "yf": "TECL",       "display": "Direxion Daily Technology Bull 3X"},
    "HIBL":    {"class": "stock",      "finnhub": "HIBL",           "yf": "HIBL",       "display": "Direxion Daily S&P 500 High Beta Bull 3X"},
    "LABU":    {"class": "stock",      "finnhub": "LABU",           "yf": "LABU",       "display": "Direxion Daily S&P Biotech Bull 3X"},
    "NVDL":    {"class": "stock",      "finnhub": "NVDL",           "yf": "NVDL",       "display": "GraniteShares 2x Long NVDA Daily ETF"},
    "BITX":    {"class": "stock",      "finnhub": "BITX",           "yf": "BITX",       "display": "2x Bitcoin Strategy ETF"},
}

# ── Timeframe mapping ─────────────────────────────────────────────────────────
# canonical -> {ccxt, yfinance, finnhub resolution}
TIMEFRAME_MAP: Dict[str, dict] = {
    "1m":  {"ccxt": "1m",  "yf": "1m",  "finnhub": 1},
    "5m":  {"ccxt": "5m",  "yf": "5m",  "finnhub": 5},
    "15m": {"ccxt": "15m", "yf": "15m", "finnhub": 15},
    "30m": {"ccxt": "30m", "yf": "30m", "finnhub": 30},
    "1h":  {"ccxt": "1h",  "yf": "1h",  "finnhub": 60},
    "4h":  {"ccxt": "4h",  "yf": "4h",  "finnhub": 240},
    "1d":  {"ccxt": "1d",  "yf": "1d",  "finnhub": "D"},
    "1w":  {"ccxt": "1w",  "yf": "1wk", "finnhub": "W"},
}


# ── Helper functions ──────────────────────────────────────────────────────────

def canonical_symbol(symbol: str) -> str:
    """Map user input (e.g. BTC/USDT, BTC/USD) to catalogue keys (BTCUSDT, BTCUSD)."""
    s = symbol.upper().replace(" ", "").replace("-", "")
    slash_aliases = {
        "BTC/USDT": "BTCUSDT",
        "BTC/USD":  "BTCUSD",
        "ETH/USDT": "ETHUSDT",
    }
    if s in slash_aliases:
        return slash_aliases[s]
    return s.replace("/", "") if "/" in s else s


def get_asset_class(symbol: str) -> str:
    """Return asset class for a canonical symbol, or 'unknown'."""
    return ASSET_CATALOGUE.get(canonical_symbol(symbol), {}).get("class", "unknown")


def get_display_name(symbol: str) -> str:
    """Return human-readable display name."""
    return ASSET_CATALOGUE.get(canonical_symbol(symbol), {}).get("display", symbol)


def get_symbols_by_class(asset_class: str) -> List[str]:
    """Return all canonical symbols for a given asset class.

    Accepts both singular (stock, commodity, index) and plural
    (stocks, commodities, indices) forms.
    """
    _alias = {
        "stocks": "stock", "commodities": "commodity", "indices": "index",
    }
    cls = _alias.get(asset_class.lower(), asset_class.lower())
    return [s for s, meta in ASSET_CATALOGUE.items()
            if meta.get("class") == cls]


def get_all_symbols() -> List[str]:
    """Return every canonical symbol in the catalogue."""
    return list(ASSET_CATALOGUE.keys())


def to_ccxt(symbol: str) -> Optional[str]:
    """Translate canonical symbol to CCXT format (crypto only)."""
    return ASSET_CATALOGUE.get(canonical_symbol(symbol), {}).get("ccxt")


def to_yfinance(symbol: str) -> Optional[str]:
    """Translate canonical symbol to yfinance ticker."""
    return ASSET_CATALOGUE.get(canonical_symbol(symbol), {}).get("yf")


def to_finnhub(symbol: str) -> Optional[str]:
    """Translate canonical symbol to Finnhub symbol string."""
    return ASSET_CATALOGUE.get(canonical_symbol(symbol), {}).get("finnhub")


def to_ibkr(symbol: str) -> Optional[str]:
    """Translate canonical symbol to IBKR format.
    
    IBKR uses canonical symbols directly for most assets.
    Returns the canonical symbol for IBKR-supported assets.
    """
    canon = canonical_symbol(symbol)
    asset_class = get_asset_class(canon)
    
    # IBKR supports stocks, forex, crypto, commodities
    if asset_class in ("stock", "forex", "crypto", "commodity"):
        return canon
    
    # Indices not directly tradeable via IBKR (use futures instead)
    return None


def ibkr_supported(symbol: str) -> bool:
    """Check if a symbol is supported by IBKR.
    
    Returns True if the symbol can be traded via IBKR.
    """
    canon = canonical_symbol(symbol)
    asset_class = get_asset_class(canon)
    
    # IBKR supports stocks, forex, crypto (limited), commodities
    # Does not support indices directly (use futures instead)
    return asset_class in ("stock", "forex", "crypto", "commodity")


def best_source(symbol: str) -> str:
    """Return the preferred data source for a symbol.

    crypto → ccxt  |  forex/commodities/indices → finnhub  |  stocks → yfinance
    
    Note: To use IBKR as a data source, explicitly pass source="ibkr" to
    get_historical_ohlcv() or get_latest_candles().
    """
    asset_class = get_asset_class(symbol)
    if asset_class == "crypto":
        return "ccxt"
    if asset_class == "stock":
        return "yfinance"
    return "finnhub"


def get_tf(timeframe: str, source: str) -> Optional[str]:
    """Return the source-specific timeframe string."""
    tf = TIMEFRAME_MAP.get(timeframe, {})
    return tf.get(source)


def add_symbol(symbol: str, asset_class: str, ccxt_id: str = "",
               finnhub_id: str = "", yf_ticker: str = "",
               display_name: str = "") -> None:
    """Dynamically register a new symbol into the catalogue at runtime."""
    ASSET_CATALOGUE[symbol.upper()] = {
        "class":   asset_class,
        "ccxt":    ccxt_id,
        "finnhub": finnhub_id,
        "yf":      yf_ticker,
        "display": display_name or symbol.upper(),
    }


def remove_symbol(symbol: str) -> bool:
    """Remove a symbol from the catalogue.  Returns True if it existed."""
    return ASSET_CATALOGUE.pop(symbol.upper(), None) is not None
