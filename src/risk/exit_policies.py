"""Mode-specific exit policies — Phase 3 / Phase 4.

Each policy encapsulates the numeric parameters that control how aggressively a
trade is protected once it is in profit.  Three concrete policies are provided:

  ScalpExitPolicy        — 3m / 5m:  fastest protection, tightest giveback,
                           ATR trail eligible after stage 2, momentum-fade tightening
  IntermediateExitPolicy — 15m / 1h: moderate (preserves Phase 2 defaults),
                           candle-based momentum-fade tightening enabled
  SwingExitPolicy        — 2h / 4h:  broadest stops, trend-following,
                           no momentum-fade tightening (too noisy on higher TFs)

The giveback fraction for IntermediateExitPolicy is deliberately kept at 0.35 so
that existing 15m/1h live trades behave exactly as they did in Phase 2.

Phase 4 additions
-----------------
Four new optional fields on ExitPolicy (all with safe defaults so existing code
that constructs ExitPolicy directly continues to work unchanged):

  trail_mode             — "candle" uses Alligator-teeth ratchet (default);
                           "atr" enables ATR-fraction trailing when eligible.
  atr_multiplier         — ATR multiple used to compute ATR trail candidate.
  atr_eligible_after_stage — Minimum profit_lock_stage before ATR trail becomes
                           eligible (default 99 = never).  SCALP overrides to 2.
  momentum_fade_window   — Number of consecutive bars to inspect for shrinking
                           bodies / momentum fade.  0 = disabled (SWING default).

Policy-state naming
-------------------
Use ``policy_state_name(policy_name, state)`` to produce consistent lifecycle
labels such as ``"SCALP_STAGE_2_LOCKED"`` or ``"INTERMEDIATE_ATR_TRAIL"``.

Formal timeframes
-----------------
``FORMAL_TIMEFRAMES`` holds the set of timeframes that map to a tuned policy.
Any timeframe outside this set falls back to IntermediateExitPolicy and should
be flagged with ``TradeRecord.used_fallback_policy = True``.

Usage::

    policy = get_exit_policy(timeframe)
    # pass to PeakGiveback:
    pg = PeakGiveback(..., giveback_frac=policy.giveback_frac)
    # richer policy state label:
    rec.exit_policy_name = policy_state_name(policy.name, "STAGE_2_LOCKED")
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Set of timeframes that map to a formally tuned exit policy ────────────────

FORMAL_TIMEFRAMES: frozenset[str] = frozenset({"1m", "3m", "5m", "15m", "1h", "2h", "4h"})


# ── Policy-state names ────────────────────────────────────────────────────────

# Valid state suffixes for policy_state_name()
_VALID_STATES = frozenset({
    "INITIAL_STOP",
    "BREAK_EVEN",
    "STAGE_1_LOCKED",
    "STAGE_2_LOCKED",
    "STAGE_3_LOCKED",
    "CANDLE_TRAIL",
    "ATR_TRAIL",
})


def policy_state_name(policy_name: str, state: str) -> str:
    """Return a canonical lifecycle label for *policy_name* + *state*.

    Examples
    --------
    >>> policy_state_name("SCALP", "STAGE_2_LOCKED")
    'SCALP_STAGE_2_LOCKED'
    >>> policy_state_name("INTERMEDIATE", "BREAK_EVEN")
    'INTERMEDIATE_BREAK_EVEN'
    """
    state_upper = state.upper()
    return f"{policy_name.upper()}_{state_upper}"


@dataclass(frozen=True)
class ExitPolicy:
    """Numeric parameters for one strategy-mode exit style.

    Attributes
    ----------
    name:
        Human-readable policy name, e.g. ``"SCALP"``.
    giveback_frac:
        Fraction of the max favorable excursion that can retrace before
        PEAK_GIVEBACK_EXIT fires (0 < giveback_frac < 1 for normal use).
    break_even_pct:
        Minimum unrealized profit (%) required before the trailing stop is
        ratcheted to entry price (break-even arm).
    profit_lock_stages:
        List of exactly 3 tuples: ``(min_profit_pct, lock_pct)``.

        * ``min_profit_pct`` — trade must reach this % unrealized gain.
        * ``lock_pct``       — total % above entry that the stop is locked to.

        Stages must be supplied in ascending order of min_profit_pct.
    trail_mode:
        ``"candle"`` — use Alligator-teeth ratchet (default for all modes).
        ``"atr"``    — switch to ATR-fraction trailing once
                       ``atr_eligible_after_stage`` is reached.
    atr_multiplier:
        ATR multiple used when ``trail_mode == "atr"``.
    atr_eligible_after_stage:
        Minimum ``profit_lock_stage`` before ATR trail is eligible.
        Default 99 means never eligible (override in SCALP to 2).
    momentum_fade_window:
        Number of consecutive closing bars to inspect for momentum fade.
        0 disables the check (default for SWING).
    """
    name:                    str
    giveback_frac:           float
    break_even_pct:          float
    profit_lock_stages:      list[tuple[float, float]] = field(default_factory=list)
    # Phase 4 fields — all have safe defaults so existing ExitPolicy(...) calls are unaffected
    trail_mode:              str   = "candle"
    atr_multiplier:          float = 1.5
    atr_eligible_after_stage:int   = 99    # 99 = never eligible
    momentum_fade_window:    int   = 0     # 0 = disabled
    # Phase 6: per-policy fade-tightening tuning — all have safe defaults so
    # existing ExitPolicy(...) calls remain unaffected
    weak_body_threshold:     float = 0.40  # body ratio below which a candle is weak
    strong_body_threshold:   float = 0.60  # body ratio above which a candle is strong
    adverse_wick_threshold:  float = 0.30  # adverse wick ≥ this = pushback signal (informational)
    fade_confirmation_bars:  int   = 1     # min weak bars in window before tightening fires
    fade_tighten_frac:       float = 0.30  # ATR fraction for candle-trail candidate stop


# ── Concrete policies ─────────────────────────────────────────────────────────

ScalpExitPolicy = ExitPolicy(
    name               = "SCALP",
    giveback_frac      = 0.25,
    break_even_pct     = 0.30,
    profit_lock_stages = [
        (1.50, 0.50),   # Stage 1: reach +1.50% → lock +0.50% above entry
        (2.00, 1.00),   # Stage 2: reach +2.00% → lock +1.00% above entry | ATR eligible
        (2.50, 1.50),   # Stage 3: reach +2.50% → lock +1.50% above entry
    ],
    trail_mode               = "atr",
    atr_multiplier           = 1.5,
    atr_eligible_after_stage = 2,    # ATR trail only after stage-2 lock is reached
    momentum_fade_window     = 3,    # inspect last 3 bars for shrinking-body fade
    fade_confirmation_bars   = 2,    # require 2 weak bars before tightening fires
    fade_tighten_frac        = 0.30, # ATR fraction for candle-trail candidate stop
)

# 1m micro-scalp policy: same staged locks but break-even arms later (+0.80%)
# because 1m noise is higher and premature break-even causes too many stop-outs.
ScalpMicroExitPolicy = ExitPolicy(
    name               = "SCALP_1M",
    giveback_frac      = 0.25,
    break_even_pct     = 0.80,   # Stage 2: arm break-even at +0.8% (1m needs more room)
    profit_lock_stages = [
        (1.50, 0.50),   # Stage 3a: reach +1.50% → lock +0.50%
        (2.00, 1.00),   # Stage 3b: reach +2.00% → lock +1.00% | ATR trail eligible
        (2.50, 1.50),   # Stage 3c: reach +2.50% → lock +1.50%
    ],
    trail_mode               = "atr",    # Stage 4: ATR trail after stage-2 lock
    atr_multiplier           = 1.5,
    atr_eligible_after_stage = 2,
    momentum_fade_window     = 3,
    fade_confirmation_bars   = 2,
    fade_tighten_frac        = 0.30,
)

IntermediateExitPolicy = ExitPolicy(
    name               = "INTERMEDIATE",
    giveback_frac      = 0.35,     # preserved from Phase 2 PEAK_GIVEBACK_FRACTION
    break_even_pct     = 0.60,
    profit_lock_stages = [
        (1.00, 0.50),
        (2.00, 1.00),
        (3.00, 1.50),
    ],
    trail_mode           = "candle",
    momentum_fade_window = 3,   # candle-quality tightening enabled for intermediate
)

SwingExitPolicy = ExitPolicy(
    name               = "SWING",
    giveback_frac      = 0.50,
    break_even_pct     = 1.00,
    profit_lock_stages = [
        (2.00, 1.00),
        (4.00, 2.00),
        (6.00, 3.00),
    ],
    trail_mode           = "candle",
    momentum_fade_window = 0,   # disabled — too noisy on higher timeframes
)

# Mapping from timeframe string to policy
_TF_POLICY: dict[str, ExitPolicy] = {
    "1m":  ScalpMicroExitPolicy,   # formal SCALP micro (1m-specific break-even at +0.8%)
    "3m":  ScalpExitPolicy,        # formal SCALP
    "5m":  ScalpExitPolicy,        # formal SCALP
    "15m": IntermediateExitPolicy, # formal INTERMEDIATE
    "30m": IntermediateExitPolicy, # informal — fallback tagged by scanner
    "1h":  IntermediateExitPolicy, # formal INTERMEDIATE
    "2h":  SwingExitPolicy,        # formal SWING
    "3h":  SwingExitPolicy,        # informal — fallback tagged by scanner
    "4h":  SwingExitPolicy,        # formal SWING
    "1d":  SwingExitPolicy,        # informal — fallback tagged by scanner
}


def get_exit_policy(timeframe: str) -> ExitPolicy:
    """Return the ExitPolicy for *timeframe*.

    Falls back to :data:`IntermediateExitPolicy` for unrecognised timeframes so
    existing behaviour is preserved for any new or unusual timeframe values.
    Callers should check ``is_formal_timeframe(timeframe)`` and set
    ``TradeRecord.used_fallback_policy = True`` when the result is False.
    """
    return _TF_POLICY.get(timeframe, IntermediateExitPolicy)
