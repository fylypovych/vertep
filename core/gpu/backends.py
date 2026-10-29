"""Configured inference adapters. No adapter returns synthetic execution output."""

import base64
import ipaddress
import json
import os
from urllib.parse import urlparse

import httpx

from .local_mode import LocalModelProvider


def local_endpoint(url):
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        # Local-only must not trust arbitrary hostnames or public DNS results.
        return False


def text_artifact(text):
    return {"filename": "response.txt", "kind": "text", "data_base64": base64.b64encode(text.encode()).decode()}


class OllamaProvider(LocalModelProvider):
    def __init__(self, name, endpoint, model, *, resources=None, timeout=300):
        self._name, self.endpoint, self.model = name, endpoint.rstrip("/"), model
        self.timeout = timeout
        self.is_local = local_endpoint(endpoint)
        super().__init__(configured=True, execute=self._infer, resources=resources)

    @property
    def name(self):
        return self._name

    @property
    def capabilities(self):
        return {"text_generation"}

    @property
    def min_vram_mb(self):
        return 0

    def query_capability(self, request):
        response = httpx.get(self.endpoint + "/api/tags", timeout=5, follow_redirects=False)
        response.raise_for_status()
        models = response.json().get("models", [])
        return {"available": request.get("type") in self.capabilities and any(item.get("name") == self.model or item.get("model") == self.model for item in models)}

    def _infer(self, task):
        response = httpx.post(self.endpoint + "/api/generate", json={"model": self.model,
            "prompt": task.get("prompt") or task.get("topic", ""), "stream": False}, timeout=self.timeout, follow_redirects=False)
        response.raise_for_status()
        text = response.json().get("response")
        if not isinstance(text, str) or not text:
            raise ValueError("Ollama returned empty output")
        return [text_artifact(text)]


class OpenAIProvider(OllamaProvider):
    is_local = False

    def __init__(self, name, endpoint, model, *, key_env, resources=None, timeout=300):
        self.key = os.environ.get(key_env)
        super().__init__(name, endpoint, model, resources=resources, timeout=timeout)
        self.is_local = False

    def _headers(self):
        if not self.key:
            raise ValueError("OpenAI credentials are not configured")
        return {"Authorization": "Bearer " + self.key}

    def query_capability(self, request):
        if request.get("type") not in self.capabilities:
            return {"available": False}
        response = httpx.get(self.endpoint + "/models", headers=self._headers(), timeout=5, follow_redirects=False)
        response.raise_for_status()
        return {"available": any(item.get("id") == self.model for item in response.json().get("data", []))}

    def _infer(self, task):
        response = httpx.post(self.endpoint + "/chat/completions", headers=self._headers(),
            json={"model": self.model, "messages": [{"role": "user", "content": task.get("prompt") or task.get("topic", "")}]},
            timeout=self.timeout, follow_redirects=False)
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
        if not text:
            raise ValueError("OpenAI returned empty output")
        return [text_artifact(text)]


class ComfyUIProvider(LocalModelProvider):
    def __init__(self, name, endpoint, execute, *, local=False, resources=None):
        self._name, self.endpoint = name, endpoint.rstrip("/")
        self.is_local = local
        super().__init__(configured=execute is not None, execute=execute, resources=resources)

    @property
    def name(self):
        return self._name

    @property
    def capabilities(self):
        return {"image_generation", "video_generation"}

    def query_capability(self, request):
        if not self._configured or request.get("type") not in self.capabilities:
            return {"available": False}
        response = httpx.get(self.endpoint + "/system_stats", timeout=5, follow_redirects=False)
        response.raise_for_status()
        return {"available": isinstance(response.json().get("devices"), list)}


class LabProvider(LocalModelProvider):
    def __init__(self, name, lease_manager, *, resources=None, local=False):
        self._name, self.manager = name, lease_manager
        self.is_local = local
        super().__init__(configured=True, execute=self._infer, resources=resources)

    @property
    def name(self):
        return self._name

    @property
    def capabilities(self):
        return {"text_generation"}

    def query_capability(self, request):
        status = self.manager.status()
        return {"available": request.get("type") in self.capabilities and status.get("gpu_available") is True}

    def _infer(self, task):
        from lab.gpu_lease import ResourceRequirement
        lease = self.manager.acquire(ResourceRequirement(model=task["model"], vram_mb=task.get("vram_mb", 0)))
        if self.is_local and not local_endpoint(lease.endpoint):
            self.manager.release()
            raise RuntimeError("Lab granted a non-local inference endpoint")
        self.manager.assert_usable()
        try:
            result = OllamaProvider(self.name, lease.endpoint, lease.model)._infer(task)
            self.manager.assert_usable()
            self.manager.release()
            return result
        except Exception:
            # Do not release uncertain remote compute merely because HTTP failed.
            self.manager.abandon("Inference outcome uncertain")
            raise


def configured_router(path, local_execute, compute_execute=None):
    from pathlib import Path
    from .provider_registry import GPUProviderRegistry
    from .router import GPURouter
    if compute_execute is None:
        from adapters.providers.compute_backends import configured_compute_execute
        compute_execute = configured_compute_execute
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    registry = GPUProviderRegistry()
    allowed = []
    for item in config["providers"]:
        name, kind = item["name"], item["type"]
        resources = item.get("resources", {})
        if kind == "local_model":
            provider = LocalModelProvider(True, execute=local_execute, resources=resources)
            provider.is_local = bool(getattr(local_execute, "is_local", False))
        elif kind == "ollama":
            provider = OllamaProvider(name, item["endpoint"], item["model"], resources=resources)
        elif kind == "openai":
            provider = OpenAIProvider(name, item["endpoint"], item["model"], key_env=item["key_env"], resources=resources)
        elif kind == "comfyui":
            provider = ComfyUIProvider(
                name, item["endpoint"],
                lambda task, config=item: compute_execute(config, task),
                local=local_endpoint(item["endpoint"]), resources=resources,
            )
        elif kind == "lab":
            from lab.gpu_lease import LeaseManager, VertepResourceClient
            manager = LeaseManager(VertepResourceClient(item["endpoint"], token=os.getenv(item.get("key_env", "")), enabled=True))
            provider = LabProvider(name, manager, resources=resources, local=local_endpoint(item["endpoint"]))
        else:
            raise ValueError("Unsupported configured GPU provider type")
        if not registry.register_provider(name, provider, priority=item["priority"]):
            raise ValueError("Duplicate configured GPU provider")
        allowed.append(name)
    return GPURouter(registry, allowed=allowed, local_only=config.get("local_only", False))
