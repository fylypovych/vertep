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
from collections.abc import Callable
from typing import Any

import httpx

SUPPORTED_BACKENDS = ("ollama", "openai")


class LLMBackendError(RuntimeError):
    """The selected LLM backend is unsupported or not usable as configured.

    Raised instead of silently switching to another backend; messages never
    carry credentials.
    """


class LLMClient(ABC):
    """Single raw-completion contract shared by all LLM backends."""

    name: str = "llm"
    _post_fn: Callable[..., Any] | None = None

    def _post(self, url: str, **kwargs: Any) -> Any:
        # ``httpx.post`` is resolved per call so a controlled transport (tests,
        # Worker provider-trace replay) observes every request.
        return (self._post_fn or httpx.post)(url, **kwargs)

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
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.url = (url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.timeout = timeout
        self._post_fn = post

    def complete(self, prompt: str, *, format_json: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if format_json:
            payload["format"] = "json"
        response = self._post(
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
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        # Same key sources the provider matrix reports as "configured".
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self._post_fn = post

    def complete(self, prompt: str, *, format_json: bool = False) -> str:
        if not self.api_key:
            raise LLMBackendError(
                "OpenAI-compatible LLM backend has no API key configured"
            )
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
            response = self._post(
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
            response = self._post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=base_payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def get_llm_client(
    *,
    purpose: str | None = None,
    timeout: float | None = None,
    post: Callable[..., Any] | None = None,
) -> LLMClient:
    """Resolve the configured LLM backend via ``VERTEP_LLM_PROVIDER``.

    ``purpose="script"`` applies the Text Worker script settings of the Ollama
    backend (``OLLAMA_SCRIPT_MODEL`` before ``OLLAMA_MODEL``, Worker default
    URL).  An unknown backend is an explicit error, never a silent Ollama.
    """
    provider = (os.getenv("VERTEP_LLM_PROVIDER") or "ollama").lower().strip()
    options: dict[str, Any] = {"post": post}
    if timeout is not None:
        options["timeout"] = timeout
    if provider == "openai":
        return OpenAICompatClient(**options)
    if provider == "ollama":
        if purpose == "script":
            options["url"] = os.getenv("OLLAMA_URL", "http://ollama:11434")
            options["model"] = os.getenv("OLLAMA_SCRIPT_MODEL") or os.getenv("OLLAMA_MODEL", "llama3.2")
        return OllamaClient(**options)
    raise LLMBackendError(
        f"Unsupported LLM backend {provider!r}; supported: {', '.join(SUPPORTED_BACKENDS)}"
    )