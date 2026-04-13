"""Asset universe registry — defines the five universe groups and provides
lookup helpers used by the prefilter layer.

Universe groups (aligned to user's tiered Core 20 structure):
    CORE_CRYPTO          — Tier 1: core crypto momentum (BTC, ETH, SOL, AVAX, …)
    CORE_MOMENTUM_STOCKS — Tier 2: US momentum equities (NVDA, TSLA, AMD, …)
    CORE_INDEX_MOMENTUM  — Tier 3: index ETFs for clean trend structure (QQQ, SPY)
    HIGH_BETA_ETFS       — Tier 4: leveraged / high-beta ETFs (TQQQ, SOXL, TECL, …)
    MEME_COIN_LANE       — Stricter lane: meme / micro-cap crypto (not part of Core 20)

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


# ── Static registry ──────────────────────────────────────────────────────────

# Tier 1 — Core crypto momentum universe (24/7 hunting ground)
# DOGE is treated as core momentum (not meme lane) due to its liquidity.
_CORE_CRYPTO_SYMBOLS: list[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD",
    "DOGE/USD", "BNB/USD", "INJ/USD", "ARB/USD", "APT/USD",
]

# Tier 2 — Core US momentum stocks (clean directional expansion)
_CORE_MOMENTUM_STOCKS: list[str] = [
    "NVDA", "TSLA", "AMD", "META", "AMZN", "NFLX", "SMCI", "AMN",
]

# Tier 3 — Index / index-style momentum (cleaner trend structure)
_CORE_INDEX_MOMENTUM: list[str] = [
    "QQQ", "SPY", "TQQQ",
]

# Tier 4 — High-beta / leveraged ETFs
_HIGH_BETA_ETFS: list[str] = [
    "SOXL", "TECL", "HIBL", "LABU", "NVDL", "BITX",
]

# Meme coin lane — stricter ATR + volume + liquidity gates (not part of Core 20)
_MEME_COIN_LANE: list[str] = [
    "SHIB/USD", "PEPE/USD", "FLOKI/USD", "WIF/USD",
    "BONK/USD", "MEME/USD", "TURBO/USD",
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
        reg[sym] = AssetEntry(sym, UniverseGroup.MEME_COIN_LANE, "crypto", is_meme=True)
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
