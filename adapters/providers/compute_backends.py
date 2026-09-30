"""Optional ComfyUI-Distributed compute backend behind ``ComputeProvider``.

Phase 4 of the open-source audit adds ``ComfyUIDistributedProvider`` as an
opt-in alternative to the default VertepWorker compute path. It is intended
for the case where a logical GPU node hosts several physical GPUs that are
orchestrated by the ComfyUI-Distributed master/proxy pattern.

Default compute remains the VertepWorker provider (attached ``ComfyUIAdapter``
/ ``DefaultComputeProvider``). The distributed cluster is used **only** when
explicitly enabled via ``VERTEP_COMPUTE_PROVIDER=comfyui-distributed`` **and** a
proxy ``COMFYUI_DISTRIBUTED_URL`` is configured; otherwise the factory falls
back to the VertepWorker provider.

Like the Phase 3 Publisher adapters, this provider never talks to ``httpx``
directly — it goes through an injectable transport so tests can drive the exact
HTTP flow with ``FakeTransport``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .base import ComputeProvider
from ._http import check_response as _check
from ..comfyui import PromptCancelledError, _substitute_placeholders
from publishers.transport import HttpTransport

class ComfyUIDistributedProvider(ComputeProvider):
    """Submit workflows to a ComfyUI-Distributed proxy endpoint.

    Exposes the same :meth:`generate_output`/``cancel`` contract as the default
    compute provider so the worker/role executor and dispatcher can switch
    transparently. Configuration:

    * ``COMFYUI_DISTRIBUTED_URL``   — base URL of the ComfyUI(-Distributed) proxy.
    * ``COMFYUI_DISTRIBUTED_TOKEN`` — optional Bearer token for the proxy.
    * ``COMFYUI_DISTRIBUTED_POLL_INTERVAL`` / ``..._TIMEOUT`` — polling tuning.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        transport=None,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
    ) -> None:
        self._url = (url or os.getenv("COMFYUI_DISTRIBUTED_URL", "")).rstrip("/")
        self._token = (
            token
            if token is not None
            else os.getenv("COMFYUI_DISTRIBUTED_TOKEN", "")
        )
        self._transport = transport or HttpTransport()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self.current_prompt_id: str | None = None
        # Prompts that could not be removed from the queue one by one.  Their late
        # result is rejected instead of being reported as a success.
        self._abandoned_prompts: set[str] = set()

    @property
    def provider(self) -> str:
        return "comfyui-distributed"

    def configured(self) -> bool:
        return bool(self._url)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self._token:
            headers.setdefault("Authorization", f"Bearer {self._token}")
        return headers

    def _load_workflow(self, workflow_path: str, topic: str) -> dict:
        """Load, validate and template an API workflow (mirrors ComfyUIAdapter)."""
        workflow_root = Path(os.getenv("WORKFLOWS_ROOT", "workflows")).resolve()
        p = Path(workflow_path)
        if p.is_absolute():
            path = p.resolve()
        elif p.parts and p.parts[0] == "workflows":
            path = (workflow_root / Path(*p.parts[1:])).resolve()
        else:
            path = (workflow_root / p).resolve()
        if path != workflow_root and workflow_root not in path.parents and not (workflow_root.name == "workflows" and workflow_root.parent in path.parents):
            raise ValueError("Workflow path escapes WORKFLOWS_ROOT")
        if not path.exists():
            raise FileNotFoundError(f"ComfyUI workflow not found: {workflow_path}")
        # Substitute inside the parsed workflow structure so arbitrary ``topic``
        # values (newlines/backslashes/quotes/Unicode) cannot corrupt JSON.
        return _substitute_placeholders(json.loads(path.read_text(encoding="utf-8")), {
            "TOPIC": topic,
            "CHECKPOINT": os.getenv("COMFYUI_CHECKPOINT", "model.safetensors"),
            "SEED": os.getenv("COMFYUI_SEED", "42"),
            "WIDTH": os.getenv("COMFYUI_WIDTH", "768"),
            "HEIGHT": os.getenv("COMFYUI_HEIGHT", "432"),
        })

    def generate_output(
        self, workflow_path: str, topic: str, task_type: str = "image"
    ) -> tuple[bytes, str, str]:
        if not self._url:
            raise RuntimeError(
                "ComfyUIDistributedProvider is not configured "
                "(set COMFYUI_DISTRIBUTED_URL)"
            )
        workflow = self._load_workflow(workflow_path, topic)

        submit = self._transport.post(
            f"{self._url}/prompt",
            headers=self._headers(),
            json={"prompt": workflow},
            timeout=30,
        )
        _check(submit)
        prompt_id = submit.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI proxy returned no prompt_id")
        self.current_prompt_id = prompt_id

        try:
            result = self._wait_for_result(prompt_id)
        finally:
            self.current_prompt_id = None
            if prompt_id not in self._abandoned_prompts:
                self._abandoned_prompts.discard(prompt_id)

        output_keys = ("images",) if task_type == "image" else ("videos", "gifs")
        for node in result.get("outputs", {}).values():
            for output_key in output_keys:
                for artifact in node.get(output_key, []):
                    view = self._transport.get(
                        f"{self._url}/view",
                        params={
                            "filename": artifact["filename"],
                            "subfolder": artifact.get("subfolder", ""),
                            "type": artifact.get("type", "output"),
                        },
                        headers=self._headers(),
                        timeout=120,
                    )
                    _check(view)
                    return view.content, Path(artifact["filename"]).name, task_type
        raise RuntimeError(f"ComfyUI proxy completed without a {task_type} output")

    def _wait_for_result(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if prompt_id in self._abandoned_prompts:
                raise PromptCancelledError(f"ComfyUI prompt was cancelled: {prompt_id}")
            response = self._transport.get(
                f"{self._url}/history/{prompt_id}",
                headers=self._headers(),
                timeout=10,
            )
            _check(response)
            history = response.json()
            if prompt_id in self._abandoned_prompts:
                raise PromptCancelledError(f"ComfyUI prompt was cancelled: {prompt_id}")
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(self._poll_interval)
        raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")

    def cancel(self) -> bool:
        """Cancel only this provider's own prompt; never the whole backend.

        ``/interrupt`` is backend-wide and would abort prompts owned by other jobs
        and other workers, so it is not used as a fallback.  A prompt that is
        already executing is abandoned and its late result is rejected.
        """
        prompt_id = self.current_prompt_id
        if not self._url:
            return False
        if not prompt_id:
            return True
        self.current_prompt_id = None
        self._abandoned_prompts.add(prompt_id)
        try:
            response = self._transport.delete(
                f"{self._url}/queue",
                params={"prompt_id": prompt_id},
                headers=self._headers(),
                timeout=10,
            )
            _check(response)
        except Exception:  # noqa: BLE001
            return False
        # The proxy answers 200 even when the prompt had already started executing, so
        # the prompt stays abandoned either way: a removal that worked makes the waiter
        # stop, and a removal that did not must not deliver a late result as success.
        return True
