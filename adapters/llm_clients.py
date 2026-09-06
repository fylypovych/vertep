"""Interchangeable LLM backends behind the LLM provider layer.

Phase 2 of the open-source audit adds an OpenAI-compatible backend next to the
default Ollama backend. Selection is configuration-driven through
``VERTEP_LLM_PROVIDER`` (``ollama`` default | ``openai``).

Both clients expose a single ``complete()`` contract that returns the raw
generated text, so orchestrators (``ScriptAgent``) stay backend-agnostic.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

import httpx


class LLMClient(ABC):
    """Single raw-completion contract shared by all LLM backends."""

    name: str = "llm"

    @abstractmethod
    def complete(self, prompt: str, *, format_json: bool = False) -> str:
        ...


class OllamaClient(LLMClient):
    """Default backend — Ollama ``/api/generate``."""

    name = "ollama"

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 300,
    ) -> None:
        self.url = (url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.timeout = timeout

    def complete(self, prompt: str, *, format_json: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if format_json:
            payload["format"] = "json"
        response = httpx.post(
            f"{self.url}/api/generate", json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json().get("response", "")


class OpenAICompatClient(LLMClient):
    """OpenAI-compatible backend — ``/v1/chat/completions`` (fleet/cloud models)."""

    name = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 300,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout

    def complete(self, prompt: str, *, format_json: bool = False) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        base_payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        payload = base_payload
        if format_json:
            payload = {**base_payload, "response_format": {"type": "json_object"}}
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            if not format_json:
                raise
            # Some OpenAI-compatible servers reject response_format — retry plain.
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=base_payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def get_llm_client() -> LLMClient:
    """Resolve the configured LLM backend via ``VERTEP_LLM_PROVIDER``."""
    provider = os.getenv("VERTEP_LLM_PROVIDER", "ollama").lower().strip()
    if provider == "openai":
        return OpenAICompatClient()
    return OllamaClient()