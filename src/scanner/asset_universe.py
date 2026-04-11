"""Asset universe registry — defines the five universe groups and provides
lookup helpers used by the prefilter layer.

Universe groups (aligned to user's tiered Core 20 structure):
    CORE_CRYPTO          — Tier 1: core crypto momentum (BTC, ETH, SOL, AVAX, SUI, …)
    CORE_MOMENTUM_STOCKS — Tier 2: US high-beta equities (NVDA, TSLA, MSTR, COIN, …)
    CORE_INDEX_MOMENTUM  — Tier 3: index ETFs for trend reference (QQQ, SPY)
    HIGH_BETA_ETFS       — Tier 4: leveraged ETFs (TQQQ, SOXL, NVDL, TSLL, BITX, …)
    MEME_COIN_LANE       — Stricter lane: meme / micro-cap crypto (not part of Core 20)

All symbol keys use canonical no-slash format matching symbol_mapper.ASSET_CATALOGUE
(e.g. ``BTCUSD`` not ``BTC/USD``) so that filter_to_universe() resolves correctly.

Each group carries:
    • a default enable/disable flag (overridable via env/config)
    • the list of canonical symbols
    • metadata: asset_class, is_meme flag

All env-var names follow the pattern ``UNIVERSE_<GROUP>_ENABLED``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.config import (
    UNIVERSE_CORE_CRYPTO_ENABLED,
    UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED,
    UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED,
    UNIVERSE_HIGH_BETA_ETFS_ENABLED,
    UNIVERSE_MEME_COIN_LANE_ENABLED,
)

log = logging.getLogger(__name__)


# ── Universe group enum ───────────────────────────────────────────────────────

class UniverseGroup(str, Enum):
    CORE_CRYPTO          = "CORE_CRYPTO"
    CORE_MOMENTUM_STOCKS = "CORE_MOMENTUM_STOCKS"
    CORE_INDEX_MOMENTUM  = "CORE_INDEX_MOMENTUM"
    HIGH_BETA_ETFS       = "HIGH_BETA_ETFS"
    MEME_COIN_LANE       = "MEME_COIN_LANE"


# ── Per-symbol metadata ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetEntry:
    symbol:          str
    universe_group:  UniverseGroup
    asset_class:     str            # "crypto" | "stock" | "etf"
    is_meme:         bool = False
    min_tf_minutes:  int  = 1       # minimum timeframe in minutes (1=any, 5=5m+, 15=15m+)


# ── Static registry ──────────────────────────────────────────────────────────

# Tier 1 — Core crypto momentum universe (24/7 hunting ground)
# Keys use canonical no-slash format (matching symbol_mapper.ASSET_CATALOGUE).
# DOGE treated as core momentum (not meme lane) due to liquidity.
# Added volatile alts (SUI, SEI, NEAR, OP, TIA, FET, RNDR, JUP) that regularly
# print 10-20% daily candles and have sufficient Kraken/Coinbase liquidity.
_CORE_CRYPTO_SYMBOLS: list[str] = [
    "BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "LINKUSD",
    "DOGEUSD", "BNBUSD", "INJUSD", "ARBUSD", "APTUSD",
    # High-volatility alts — frequent 10-20% moves
    "SUIUSD", "SEIUSD", "NEARUSD", "OPUSD",
    "TIAUSD", "FETUSD", "RNDRUSD", "JUPUSD",
]

# Tier 2 — Core US momentum stocks (clean directional expansion)
# Added high-beta names: MSTR (BTC proxy), COIN/MARA/RIOT (crypto equity),
# PLTR (gov-AI momentum), HOOD (retail flow). All regularly move 5-15% daily.
_CORE_MOMENTUM_STOCKS: list[str] = [
    "NVDA", "TSLA", "AMD", "META", "AMZN", "NFLX", "SMCI",
    # High-beta additions
    "MSTR", "COIN", "MARA", "RIOT", "PLTR", "HOOD",
]

# Tier 3 — Index ETFs (clean trend structure reference only)
# TQQQ moved out — it's a 3x leveraged ETF, belongs in HIGH_BETA_ETFS.
_CORE_INDEX_MOMENTUM: list[str] = [
    "QQQ", "SPY",
]

# Tier 4 — High-beta / leveraged ETFs (now enabled by default — bot is tuned)
# TQQQ moved here from Tier 3 where it didn't belong.
# NVDL (2x NVDA), TSLL (2x TSLA), BITX (2x BTC) add leveraged single-stock exposure.
_HIGH_BETA_ETFS: list[str] = [
    "TQQQ", "SOXL", "TECL", "HIBL", "LABU",
    "NVDL", "TSLL", "BITX",
]

# Meme coin lane — stricter ATR + volume + liquidity gates (not part of Core 20)
_MEME_COIN_LANE: list[str] = [
    "SHIBUSD", "PEPEUSD", "FLOKIUSD", "WIFUSD",
    "BONKUSD", "MEMEUSD", "TURBOUSD",
]


def _build_registry() -> dict[str, AssetEntry]:
    """Build the full symbol → AssetEntry mapping."""
    reg: dict[str, AssetEntry] = {}
    for sym in _CORE_CRYPTO_SYMBOLS:
        reg[sym] = AssetEntry(sym, UniverseGroup.CORE_CRYPTO, "crypto")
    for sym in _CORE_MOMENTUM_STOCKS:
        reg[sym] = AssetEntry(sym, UniverseGroup.CORE_MOMENTUM_STOCKS, "stock")
    for sym in _CORE_INDEX_MOMENTUM:
        reg[sym] = AssetEntry(sym, UniverseGroup.CORE_INDEX_MOMENTUM, "etf")
    for sym in _HIGH_BETA_ETFS:
        reg[sym] = AssetEntry(sym, UniverseGroup.HIGH_BETA_ETFS, "etf")
    for sym in _MEME_COIN_LANE:
        reg[sym] = AssetEntry(sym, UniverseGroup.MEME_COIN_LANE, "crypto", is_meme=True, min_tf_minutes=5)
    return reg


_REGISTRY: dict[str, AssetEntry] = _build_registry()


# ── Group enable flags ────────────────────────────────────────────────────────

_GROUP_ENABLED: dict[UniverseGroup, bool] = {
    UniverseGroup.CORE_CRYPTO:          UNIVERSE_CORE_CRYPTO_ENABLED,
    UniverseGroup.CORE_MOMENTUM_STOCKS: UNIVERSE_CORE_MOMENTUM_STOCKS_ENABLED,
    UniverseGroup.CORE_INDEX_MOMENTUM:  UNIVERSE_CORE_INDEX_MOMENTUM_ENABLED,
    UniverseGroup.HIGH_BETA_ETFS:       UNIVERSE_HIGH_BETA_ETFS_ENABLED,
    UniverseGroup.MEME_COIN_LANE:       UNIVERSE_MEME_COIN_LANE_ENABLED,
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_entry(symbol: str) -> Optional[AssetEntry]:
    """Return the AssetEntry for *symbol*, or None if not in the registry."""
    return _REGISTRY.get(symbol)


def is_known(symbol: str) -> bool:
    """Return True when *symbol* is part of the managed universe."""
    return symbol in _REGISTRY


def is_meme(symbol: str) -> bool:
    """Return True when *symbol* is tagged as a meme coin."""
    entry = _REGISTRY.get(symbol)
    return entry.is_meme if entry else False


def universe_group(symbol: str) -> Optional[UniverseGroup]:
    """Return the UniverseGroup for *symbol*, or None."""
    entry = _REGISTRY.get(symbol)
    return entry.universe_group if entry else None


def asset_class(symbol: str) -> Optional[str]:
    """Return 'crypto' | 'stock' | 'etf' for *symbol*, or None."""
    entry = _REGISTRY.get(symbol)
    return entry.asset_class if entry else None


def is_group_enabled(group: UniverseGroup) -> bool:
    """Return whether *group* is enabled in config."""
    return _GROUP_ENABLED.get(group, False)


def get_enabled_symbols() -> list[str]:
    """Return all symbols belonging to currently-enabled universe groups."""
    return [
        sym for sym, entry in _REGISTRY.items()
        if _GROUP_ENABLED.get(entry.universe_group, False)
    ]


def get_symbols_for_group(group: UniverseGroup) -> list[str]:
    """Return all symbols in a given universe group (regardless of enabled)."""
    return [sym for sym, entry in _REGISTRY.items() if entry.universe_group == group]


def filter_to_universe(symbols: list[str]) -> list[str]:
    """From an arbitrary symbol list, keep only those in an enabled group.

    Symbols not in the registry at all are passed through unchanged (allows
    the operator to inject custom ad-hoc symbols without universe gating).
    """
    result: list[str] = []
    for sym in symbols:
        entry = _REGISTRY.get(sym)
        if entry is None:
            # Unknown symbol — let it through (operator override)
            result.append(sym)
        elif _GROUP_ENABLED.get(entry.universe_group, False):
            result.append(sym)
        else:
            log.debug("Universe filter: %s blocked (group %s disabled)", sym, entry.universe_group.value)
    return result


def all_groups() -> list[UniverseGroup]:
    """Return all universe group enums."""
    return list(UniverseGroup)


def registry_snapshot() -> dict[str, AssetEntry]:
    """Return a shallow copy of the full registry (for reporting)."""
    return dict(_REGISTRY)


# ── Timeframe suitability ─────────────────────────────────────────────────────

_TF_MINUTES: dict[str, int] = {
    "1m": 1, "2m": 2, "3m": 3, "5m": 5, "15m": 15,
    "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


def is_suitable_for_timeframe(symbol: str, timeframe: str) -> bool:
    """Return False if this asset should not be traded on this timeframe.

    Meme coins require at least 5m (too noisy on 1m/3m).
    """
    entry = get_entry(symbol)
    if entry is None:
        return True  # unknown asset — allow, fail-open
    tf_mins = _TF_MINUTES.get(timeframe, 60)
    return tf_mins >= entry.min_tf_minutes
