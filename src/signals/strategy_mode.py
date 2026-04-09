"""Strategy mode — canonical mapping from timeframe to strategy mode.

SCALP        — 1m, 3m, 5m      (fast setups, noise-sensitive; 1m = micro-scalp)
INTERMEDIATE — 15m, 30m, 1h   (medium-term follow-through)
SWING        — 2h, 3h, 4h     (structural moves, wider range)
UNKNOWN      — any other timeframe (excluded from mode-grouped analytics)

Phase 4 additions
-----------------
``FORMAL_TIMEFRAMES`` — frozenset of the six timeframes that map to a
    formally tuned exit policy (3m, 5m, 15m, 1h, 2h, 4h).  Defined here
    (not imported from exit_policies) to avoid circular imports.

``is_formal_timeframe(tf)`` — returns True for the six formal TFs, False
    for all others (1m, 30m, 3h, 1d, …).  When False, the scanner should
    set ``TradeRecord.used_fallback_policy = True`` and log a WARNING so
    operators know the trade is using a fallback exit policy.

The formal set purposely excludes 30m, 3h, 1d (they fall back to
INTERMEDIATE/SWING) so that architecture can be tightened later without
changing the mapping logic.
"""

from __future__ import annotations

from enum import Enum


class StrategyMode(str, Enum):
    """String-valued enum so instances serialise directly to TEXT in SQLite."""
    SCALP        = "SCALP"
    INTERMEDIATE = "INTERMEDIATE"
    SWING        = "SWING"
    UNKNOWN      = "UNKNOWN"


# Seven formally-tuned timeframes.  Any timeframe outside this set receives the
# fallback INTERMEDIATE policy and is tagged with used_fallback_policy=True.
# Defined locally to avoid a circular import with src.risk.exit_policies.
FORMAL_TIMEFRAMES: frozenset[str] = frozenset({"1m", "3m", "5m", "15m", "1h", "2h", "4h"})


_TIMEFRAME_MAP: dict[str, StrategyMode] = {
    "1m":  StrategyMode.SCALP,
    "3m":  StrategyMode.SCALP,
    "5m":  StrategyMode.SCALP,
    "15m": StrategyMode.INTERMEDIATE,
    "30m": StrategyMode.INTERMEDIATE,
    "1h":  StrategyMode.INTERMEDIATE,
    "2h":  StrategyMode.SWING,
    "3h":  StrategyMode.SWING,
    "4h":  StrategyMode.SWING,
}


def timeframe_to_mode(timeframe: str) -> StrategyMode:
    """Return the StrategyMode for the given timeframe string.

    Unrecognised timeframes return StrategyMode.UNKNOWN and are excluded
    from mode-grouped performance analytics.
    """
    return _TIMEFRAME_MAP.get(timeframe, StrategyMode.UNKNOWN)


def is_formal_timeframe(timeframe: str) -> bool:
    """Return True if *timeframe* belongs to the set of formally-tuned modes.

    Formal timeframes: 3m, 5m, 15m, 1h, 2h, 4h.

    Any other timeframe (1m, 30m, 3h, 1d, …) maps to a fallback policy.
    Callers that return False should set ``TradeRecord.used_fallback_policy=True``
    and emit a WARNING log so operators have visibility.
    """
    return timeframe in FORMAL_TIMEFRAMES
