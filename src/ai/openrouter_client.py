"""OpenRouter client — Kimi K2 fallback AI.

Used when LM Studio is unavailable.
Model: moonshotai/kimi-k2 via openrouter.ai

Budget: ~$10 OpenRouter credit.
API docs: https://openrouter.ai/docs
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

try:
    from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL
except ImportError:
    OPENROUTER_API_KEY = ""
    OPENROUTER_MODEL   = "moonshotai/kimi-k2"

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_REFERER        = "https://github.com/algobot"


class OpenRouterClient:
    """Kimi K2 via OpenRouter — backup AI confidence scorer."""

    def __init__(
        self,
        api_key:  str = OPENROUTER_API_KEY,
        model:    str = OPENROUTER_MODEL,
        timeout:  int = 45,
    ) -> None:
        self.api_key = api_key
        self.model   = model
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _chat(self, messages: list[dict], temperature: float = 0.1) -> Optional[str]:
        if not self.api_key:
            log.debug("OpenRouter API key not set")
            return None
        try:
            resp = requests.post(
                _OPENROUTER_URL,
                headers={
                    "Authorization":  f"Bearer {self.api_key}",
                    "HTTP-Referer":   _REFERER,
                    "X-Title":        "AlgoBot",
                    "Content-Type":   "application/json",
                },
                json={
                    "model":       self.model,
                    "messages":    messages,
                    "temperature": temperature,
                    "max_tokens":  256,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                log.warning("OpenRouter returned %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error("OpenRouter request failed: %s", e)
            return None

    def score_signal(self, prompt: str) -> Optional[float]:
        """Same interface as LMStudioClient.score_signal.

        Returns 0.0–1.0 confidence float or None.
        """
        system_msg = (
            "You are a professional algorithmic trading assistant. "
            "Analyse the given trading signal and respond ONLY with a confidence "
            "score between 0 and 100 (integer). No other text. "
            "Higher = more confident the signal leads to profit."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": prompt},
        ]
        response = self._chat(messages, temperature=0.0)
        if response is None:
            return None
        match = re.search(r"\d{1,3}", response)
        if match:
            score = int(match.group())
            return max(0, min(100, score)) / 100.0
        log.warning("OpenRouter non-numeric response: %s", response[:80])
        return None

    def debrief(self, summary: str) -> Optional[str]:
        system_msg = (
            "You are a professional algorithmic trading analyst. "
            "Review the trade summary and give a concise debrief under 300 words: "
            "what worked, what didn't, and 3 actionable suggestions."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": summary},
        ]
        return self._chat(messages, temperature=0.3)
