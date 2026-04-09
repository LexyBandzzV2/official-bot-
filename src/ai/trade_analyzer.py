"""Post-trade AI analysis — captures lessons from every closed trade.

After every trade closes, this module sends the trade context to the AI
and receives a pattern label + contextual notes for later aggregation.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.ai.lm_studio_client  import LMStudioClient
from src.ai.openrouter_client import OpenRouterClient

log = logging.getLogger(__name__)

_lm: Optional[LMStudioClient]    = None
_or: Optional[OpenRouterClient]   = None


def _get_lm() -> LMStudioClient:
    global _lm
    if _lm is None:
        _lm = LMStudioClient()
    return _lm


def _get_or() -> OpenRouterClient:
    global _or
    if _or is None:
        _or = OpenRouterClient()
    return _or


def _build_prompt(trade: dict) -> str:
    return (
        "You are an expert trading analyst. Analyse the following closed trade and "
        "provide:\n"
        "1. A short pattern label (e.g. 'trending_breakout_success', "
        "'mean_reversion_failure', 'range_bound_false_signal')\n"
        "2. A brief contextual note (1-3 sentences)\n\n"
        f"Trade details:\n"
        f"  Direction:      {trade.get('signal_type', '?')}\n"
        f"  Asset:          {trade.get('asset', '?')}\n"
        f"  Entry price:    {trade.get('entry_price', 0)}\n"
        f"  Exit price:     {trade.get('exit_price', 0)}\n"
        f"  Close reason:   {trade.get('close_reason', '?')}\n"
        f"  PnL %:          {trade.get('pnl_pct', 0):+.2f}%\n"
        f"  Duration:       {trade.get('duration', 'unknown')}\n"
        f"  Alligator:      {'✓' if trade.get('alligator_pt') else '✗'}\n"
        f"  Stochastic:     {'✓' if trade.get('stochastic_pt') else '✗'}\n"
        f"  Vortex:         {'✓' if trade.get('vortex_pt') else '✗'}\n"
        f"  ML confidence:  {trade.get('ml_confidence', 'N/A')}\n"
        f"  AI confidence:  {trade.get('ai_confidence', 'N/A')}\n\n"
        "Respond in the exact format:\n"
        "LABEL: <label>\n"
        "NOTE: <note>"
    )


def analyse_trade(trade: dict) -> Optional[dict]:
    """Send a closed trade to the AI for pattern analysis.

    Parameters
    ----------
    trade : dict with keys matching the trades DB row.

    Returns
    -------
    dict with keys ``label`` and ``note``, or None if AI unavailable.
    """
    prompt = _build_prompt(trade)

    response: Optional[str] = None
    lm = _get_lm()
    if lm.is_available():
        try:
            response = lm.debrief(prompt)
        except Exception as exc:
            log.warning("LM Studio trade analysis failed: %s", exc)

    if response is None:
        or_client = _get_or()
        if or_client.is_available():
            try:
                response = or_client.debrief(prompt)
            except Exception as exc:
                log.warning("OpenRouter trade analysis failed: %s", exc)

    if response is None:
        return None

    # Parse response
    label = "unknown"
    note  = response.strip()
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("LABEL:"):
            label = stripped[6:].strip().lower().replace(" ", "_")
        elif stripped.upper().startswith("NOTE:"):
            note = stripped[5:].strip()

    return {"label": label, "note": note}


