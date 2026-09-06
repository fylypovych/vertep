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
        path = Path(workflow_path).resolve()
        workflow_root = Path(os.getenv("WORKFLOWS_ROOT", "workflows")).resolve()
        if workflow_root not in path.parents:
            raise ValueError("Workflow path escapes WORKFLOWS_ROOT")
        if not path.exists():
            raise FileNotFoundError(f"ComfyUI workflow not found: {workflow_path}")
        serialized = path.read_text(encoding="utf-8")
        serialized = serialized.replace("{{TOPIC}}", topic.replace('"', '\\"'))
        serialized = serialized.replace(
            "{{CHECKPOINT}}", os.getenv("COMFYUI_CHECKPOINT", "model.safetensors")
        )
        serialized = serialized.replace("{{SEED}}", os.getenv("COMFYUI_SEED", "42"))
        serialized = serialized.replace("{{WIDTH}}", os.getenv("COMFYUI_WIDTH", "768"))
        serialized = serialized.replace("{{HEIGHT}}", os.getenv("COMFYUI_HEIGHT", "432"))
        return json.loads(serialized)

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
            response = self._transport.get(
                f"{self._url}/history/{prompt_id}",
                headers=self._headers(),
                timeout=10,
            )
            _check(response)
            history = response.json()
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(self._poll_interval)
        raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")

    def cancel(self) -> bool:
        if not self._url:
            return False
        try:
            response = self._transport.post(
                f"{self._url}/interrupt",
                headers=self._headers(),
                timeout=10,
            )
            _check(response)
            return True
        except Exception:  # noqa: BLE001
            return False
