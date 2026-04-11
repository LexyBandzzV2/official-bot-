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
    # Minimum favorable excursion (as a fraction of entry price) required before the
    # peak-giveback exit is eligible to fire.  Prevents exiting at a loss when the
    # move was trivially small (e.g. a doji that barely nudged past entry on noise).
    # 0.003 = 0.3%: the peak must reach entry × 1.003 before giveback can trigger.
    min_mfe_pct:             float = 0.003


# ── Concrete policies ─────────────────────────────────────────────────────────

# ── EXIT POLICY PHILOSOPHY ────────────────────────────────────────────────────
# Target: 1–20% profit per trade on volatile assets (crypto, MSTR, SOXL, etc.)
#
# Root-cause of 0.05% exits (the bug we're fixing):
#   Old Stage 1 was (reach +0.50% → lock +0.15%).  With giveback_frac=0.45,
#   the giveback trigger fired at entry+0.275%, and the profit lock floor was
#   entry+0.15%.  Either way the trade closed at <0.30% — netting ~0.05% after fees.
#
# Fix: stages are now set to multiples of real high-beta daily ranges (1–15%).
#   • break_even_pct raised so we don't arm the floor on tiny noise
#   • Stage 1 triggers at meaningful profit levels (1.5–4%) not 0.50%
#   • giveback_frac kept at 0.40 — lets 60% of the move develop before exit
#   • min_mfe_pct raised in proportion to the new stage levels
# ─────────────────────────────────────────────────────────────────────────────

# 3m / 5m scalp: target 1–6% moves; trail with ATR after stage 2.
# Lock ratios: Stage1=47%, Stage2=67%, Stage3=75% of the move secured.
ScalpExitPolicy = ExitPolicy(
    name               = "SCALP",
    giveback_frac      = 0.35,   # exit when 35% of peak retraces — keeps 65% of move
    break_even_pct     = 0.50,   # arm break-even at +0.50% (was 0.60 — arm sooner)
    min_mfe_pct        = 0.008,  # peak must reach +0.8% before giveback can fire
    profit_lock_stages = [
        (1.50, 0.70),   # Stage 1: reach +1.50% → lock +0.70% (47% secured, was 33%)
        (3.00, 2.00),   # Stage 2: reach +3.00% → lock +2.00% (67% secured, was 50%) | ATR trail eligible
        (6.00, 4.50),   # Stage 3: reach +6.00% → lock +4.50% (75% secured, was 50%)
    ],
    trail_mode               = "atr",
    atr_multiplier           = 1.0,   # tighter ATR trail (was 1.2)
    atr_eligible_after_stage = 2,
    momentum_fade_window     = 3,
    fade_confirmation_bars   = 1,     # act on first weak candle (was 2 — too slow for scalp)
    fade_tighten_frac        = 0.25,  # tighter fade stop (was 0.30)
)

# 1m micro-scalp: faster timeframe — tighter stages but still targeting real moves.
# Lock ratios: Stage1=47%, Stage2=67%, Stage3=75%.
ScalpMicroExitPolicy = ExitPolicy(
    name               = "SCALP_1M",
    giveback_frac      = 0.35,   # exit when 35% of peak retraces (was 0.40)
    break_even_pct     = 0.30,   # arm break-even at +0.30% (was 0.40 — arm sooner)
    min_mfe_pct        = 0.005,  # peak must reach +0.5% before giveback can fire
    profit_lock_stages = [
        (0.75, 0.35),   # Stage 1: reach +0.75% → lock +0.35% (47% secured, was 33%)
        (1.50, 1.00),   # Stage 2: reach +1.50% → lock +1.00% (67% secured, was 50%) | ATR trail eligible
        (3.00, 2.25),   # Stage 3: reach +3.00% → lock +2.25% (75% secured, was 50%)
    ],
    trail_mode               = "atr",
    atr_multiplier           = 1.0,   # tight ATR trail (was 1.2)
    atr_eligible_after_stage = 2,
    momentum_fade_window     = 3,
    fade_confirmation_bars   = 1,     # 1m moves fast — act on first weak candle
    fade_tighten_frac        = 0.20,  # very tight fade stop on 1m
)

# 15m / 1h intermediate: target 2–8% moves; candle trail keeps momentum alive.
# Lock ratios: Stage1=50%, Stage2=69%, Stage3=75%.
IntermediateExitPolicy = ExitPolicy(
    name               = "INTERMEDIATE",
    giveback_frac      = 0.35,   # exit when 35% of peak retraces (was 0.40)
    break_even_pct     = 0.80,   # arm break-even at +0.80% (was 1.00 — arm sooner)
    min_mfe_pct        = 0.012,  # peak must reach +1.2% before giveback fires
    profit_lock_stages = [
        (2.00, 1.00),   # Stage 1: reach +2.00% → lock +1.00% (50% secured, was 38%)
        (4.00, 2.75),   # Stage 2: reach +4.00% → lock +2.75% (69% secured, was 50%)
        (8.00, 6.00),   # Stage 3: reach +8.00% → lock +6.00% (75% secured, was 50%)
    ],
    trail_mode           = "candle",
    momentum_fade_window = 3,
    fade_confirmation_bars = 2,   # 15m/1h: 2-bar confirmation appropriate (each bar = 15-60 min)
)

# 2h / 4h swing: target 5–20% moves; wide room to run full trend.
# Lock ratios: Stage1=50%, Stage2=69%, Stage3=77%.
SwingExitPolicy = ExitPolicy(
    name               = "SWING",
    giveback_frac      = 0.32,   # exit when 32% of peak retraces — keeps 68% (was 0.38)
    break_even_pct     = 1.20,   # arm break-even at +1.20% (was 1.50 — arm sooner)
    min_mfe_pct        = 0.018,  # peak must reach +1.8% before giveback fires
    profit_lock_stages = [
        (4.00,  2.00),   # Stage 1: reach +4.00%  → lock +2.00%  (50% secured, was 38%)
        (8.00,  5.50),   # Stage 2: reach +8.00%  → lock +5.50%  (69% secured, was 50%)
        (15.00, 11.50),  # Stage 3: reach +15.00% → lock +11.50% (77% secured, was 53%)
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
