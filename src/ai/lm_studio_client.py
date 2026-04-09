"""LM Studio client — OpenAI-compatible interface to the local model server.

LM Studio exposes an OpenAI-compatible REST API at http://localhost:1234/v1
This module wraps that API for signal confidence scoring.

Usage:
    from src.ai.lm_studio_client import LMStudioClient
    client = LMStudioClient()
    score = client.score_signal(prompt)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

try:
    from src.config import LM_STUDIO_URL, LM_STUDIO_MODEL
except ImportError:
    LM_STUDIO_URL   = "http://localhost:1234/v1"
    LM_STUDIO_MODEL = "llama3.1:8b"


class LMStudioClient:
    """Thin wrapper around LM Studio's OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str  = LM_STUDIO_URL,
        model:    str  = LM_STUDIO_MODEL,
        timeout:  int  = 30,
    ) -> None:
        base = (base_url or "http://localhost:1234/v1").rstrip("/")
        # Accept either host root (http://127.0.0.1:1234) or OpenAI path (.../v1).
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self.base_url = base
        self.model    = model
        self.timeout  = timeout

    def _chat(self, messages: list[dict], temperature: float = 0.1) -> Optional[str]:
        """Send a chat completion request. Returns the assistant message or None."""
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Content-Type": "application/json"},
                json={
                    "model":       self.model,
                    "messages":    messages,
                    "temperature": temperature,
                    "max_tokens":  256,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                log.warning("LM Studio returned %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.ConnectionError:
            log.warning("LM Studio not reachable at %s", self.base_url)
        except Exception as e:
            log.error("LM Studio request failed: %s", e)
        return None

    def is_available(self) -> bool:
        """Quick health check — returns True if LM Studio is running."""
        try:
            resp = requests.get(f"{self.base_url}/models", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def score_signal(self, prompt: str) -> Optional[float]:
        """Ask the model to rate the signal quality.

        Returns a float between 0.0 and 1.0, or None if unavailable.
        The prompt should contain full signal context.
        """
        system_msg = (
            "You are a professional algorithmic trading assistant. "
            "Analyse the given trading signal and respond ONLY with a confidence "
            "score between 0 and 100 (integer). Do not include any other text. "
            "Higher = more confident the signal will be profitable. "
            "Consider: trend alignment, indicator confluence, market context."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": prompt},
        ]
        response = self._chat(messages, temperature=0.0)
        if response is None:
            return None
        # Extract first integer from response
        import re
        match = re.search(r"\d{1,3}", response)
        if match:
            score = int(match.group())
            score = max(0, min(100, score))
            return score / 100.0
        log.warning("LM Studio returned non-numeric response: %s", response[:80])
        return None

    def debrief(self, summary: str) -> Optional[str]:
        """Ask the model to provide a trading debrief / weekly analysis.

        Returns the model's full text response.
        """
        system_msg = (
            "You are a professional algorithmic trading analyst. "
            "Review the provided trade summary and give a concise debrief: "
            "what worked, what didn't, and 3 actionable suggestions. "
            "Keep your response under 300 words."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": summary},
        ]
        return self._chat(messages, temperature=0.3)
