"""AI signal ranker — builds signal prompt and queries LM Studio or Kimi K2.

Workflow:
  1. Receive a BuySignalResult or SellSignalResult
  2. Build a rich natural-language context string
  3. Query LM Studio first (localhost:1234)
  4. If LM Studio is unavailable, fall back to OpenRouter / Kimi K2
  5. Return confidence score 0.0–1.0 and attach it to the signal

The threshold for passing a signal is AI_CONFIDENCE_THRESHOLD (default 0.60).
"""

from __future__ import annotations

import logging
from typing import Optional, Union

log = logging.getLogger(__name__)

try:
    from src.config import AI_CONFIDENCE_THRESHOLD
    from src.ai.lm_studio_client  import LMStudioClient
    from src.ai.openrouter_client import OpenRouterClient
except ImportError:
    AI_CONFIDENCE_THRESHOLD = 0.60
    LMStudioClient    = None   # type: ignore
    OpenRouterClient  = None   # type: ignore

# Module-level client singletons (lazy initialised)
_lm_client: Optional[LMStudioClient]   = None
_or_client: Optional[OpenRouterClient] = None


def _get_lm() -> LMStudioClient:
    global _lm_client
    if _lm_client is None:
        _lm_client = LMStudioClient()
    return _lm_client


def _get_or() -> OpenRouterClient:
    global _or_client
    if _or_client is None:
        _or_client = OpenRouterClient()
    return _or_client


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(sig: object) -> str:
    """Construct a human-readable signal context string for the LLM."""
    sl  = sig       # type: ignore
    direction = sl.signal_type  # "BUY" or "SELL"
    lines = [
        f"TRADING SIGNAL ANALYSIS REQUEST",
        f"",
        f"Signal Type:  {direction}",
        f"Asset:        {sl.asset}",
        f"Timeframe:    {sl.timeframe}",
        f"Timestamp:    {sl.timestamp.isoformat()}",
        f"",
        f"--- INDICATOR CONFLUENCE ({sl.points}/3 points) ---",
        f"Alligator:    {'✓ PASS' if sl.alligator_point else '✗ FAIL'}",
        f"Stochastic:   {'✓ PASS' if sl.stochastic_point else '✗ FAIL'}",
        f"Vortex:       {'✓ PASS' if sl.vortex_point else '✗ FAIL'}",
            # Staircase logic removed
        f"",
        f"--- PRICE LEVELS ---",
        f"Entry Price:  {sl.entry_price:.5f}",
        f"Stop Loss:    {sl.stop_loss:.5f}  (-2% hard floor)",
        f"Alligator Jaw:    {sl.jaw_price:.5f}",
        f"Alligator Teeth:  {sl.teeth_price:.5f}  (trailing stop track)",
        f"Alligator Lips:   {sl.lips_price:.5f}",
        f"",
        f"--- CONTEXT ---",
        f"All indicators are calculated on Heikin Ashi candles (noise-reduced).",
        f"Exit condition: Alligator lips crosses {('down to' if direction == 'BUY' else 'up to')} teeth.",
        f"Trailing stop ratchets with the Teeth line (never moves against trade).",
        f"",
        f"Based only on the indicator confluence and price levels above, "
        f"provide a confidence score (0-100) for this {direction} signal being profitable.",
    ]
    return "\n".join(lines)


# ── Public scorer ─────────────────────────────────────────────────────────────

def rank_signal(sig: object) -> float:
    """Score a signal using AI. Returns confidence 0.0–1.0.

    Tries LM Studio first; falls back to OpenRouter/Kimi K2.
    Returns 0.5 (neutral) if both are unavailable (no hard rejection).
    """
    prompt = _build_prompt(sig)

    # Try LM Studio (local, free, fast)
    lm = _get_lm()
    if lm.is_available():
        score = lm.score_signal(prompt)
        if score is not None:
            log.debug("LM Studio scored %s %s → %.0f%%", sig.signal_type, sig.asset, score * 100)  # type: ignore
            return score

    # Fall back to OpenRouter / Kimi K2
    or_client = _get_or()
    if or_client.is_available():
        score = or_client.score_signal(prompt)
        if score is not None:
            log.debug("Kimi K2 scored %s %s → %.0f%%", sig.signal_type, sig.asset, score * 100)  # type: ignore
            return score

    log.info("No AI client available — returning neutral score 0.5 for %s %s",
             sig.signal_type, sig.asset)  # type: ignore
    return 0.5


def run_debrief(trade_summary: str) -> Optional[str]:
    """Run an AI debrief on a trade or session summary string.

    Returns the full analysis text, or None if both AI clients are unavailable.
    """
    lm = _get_lm()
    if lm.is_available():
        return lm.debrief(trade_summary)
    or_client = _get_or()
    if or_client.is_available():
        return or_client.debrief(trade_summary)
    return None
