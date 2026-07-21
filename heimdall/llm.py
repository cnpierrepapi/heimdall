"""OpenAI-compatible chat client for the worker agents.

Defaults to an open-weight model on OpenRouter; any OpenAI-compatible
endpoint works via LLM_BASE_URL / LLM_MODEL / LLM_API_KEY. Agents ask for
strict JSON and this client enforces it: reasoning disabled where the
provider honors it, <think> blocks stripped where it does not, then parse
with bounded retries.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-32b"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise LLMError("no API key: set LLM_API_KEY or OPENROUTER_API_KEY")
        self._client = httpx.Client(timeout=timeout)

    def chat(self, system: str, user: str, max_tokens: int = 2000) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            # honored by OpenRouter for reasoning-capable models; harmless elsewhere
            "reasoning": {"enabled": False},
        }
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
        )
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
        content = resp.json()["choices"][0]["message"]["content"] or ""
        return _THINK_RE.sub("", content).strip()

    def chat_json(
        self, system: str, user: str, max_tokens: int = 2000, retries: int = 2
    ) -> Any:
        """Chat expecting a JSON body; strips fences and retries on bad parses."""
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            text = self.chat(system, user, max_tokens=max_tokens)
            cleaned = _FENCE_RE.sub("", text).strip()
            # tolerate prose around a JSON object
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                cleaned = cleaned[start : end + 1]
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                last_err = e
                user = (
                    user
                    + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
                )
                time.sleep(1.0 * (attempt + 1))
        raise LLMError(f"no valid JSON after {retries + 1} attempts: {last_err}")

    def close(self) -> None:
        self._client.close()
