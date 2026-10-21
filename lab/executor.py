"""Role executor client — the only path from a run to a real role invocation.

``Issue i.0.0.2.50`` (L1/L2) requires that a laboratory task may only reach
``COMPLETED`` after an actual invocation of the approved role stack (the
controlled OpenCode / OmniRoute endpoint), never from a fixture, a stub or a
silent fake provider.  The contract is deliberately small:

* the caller passes the queued task, the model, the provider and the
  isolated workspace; the client sends them as JSON to ``<endpoint>/invoke``;
* a disabled client, a missing endpoint, an unreachable endpoint, a refused
  request or a malformed answer all raise — they never return a plausible
  result, so the orchestrator reports ``FAILED`` instead of ``COMPLETED``;
* production runs are fail-closed: the endpoint must be enabled explicitly
  (``LAB_ROLE_EXECUTOR_ENABLED=true`` or an explicit ``enabled=True``), and
  the workspace must pass its isolation check before any bytes leave the host.

The server side of the contract is the laboratory's own runner; tests exercise
the client against a real local HTTP endpoint, not against a monkeypatched
function (Issue i.0.0.2.50, L6).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ROLE_EXECUTOR_CONTRACT = "role-executor/1"


class ExecutorError(RuntimeError):
    """The role executor refused, failed or answered outside the contract."""


class ExecutorUnavailable(ExecutorError):
    """No enabled role executor is configured — a run must not pretend otherwise."""


class EndpointRoleExecutor:
    """HTTP client for the controlled role-execution endpoint."""

    def __init__(self, endpoint: str, *, token: str | None = None,
                 timeout: float = 60.0, enabled: bool | None = None,
                 opener=None, contract_version: str = ROLE_EXECUTOR_CONTRACT):
        self.endpoint = str(endpoint or "").rstrip("/")
        self.token = token if token is not None else os.getenv("LAB_ROLE_EXECUTOR_TOKEN", "")
        if enabled is None:
            enabled = os.getenv("LAB_ROLE_EXECUTOR_ENABLED", "").lower() in {"true", "1", "yes"}
        self.enabled = bool(enabled)
        self.timeout = timeout
        self.contract_version = contract_version
        self._opener = opener or urllib.request.urlopen

    def __call__(self, task, *, model: str = "", provider: str = "",
                 workspace=None) -> dict:
        """Invoke the role endpoint once and return its verified response."""
        if not self.enabled:
            raise ExecutorUnavailable(
                "role executor is disabled (set LAB_ROLE_EXECUTOR_ENABLED=true "
                "or pass enabled=True)")
        if not self.endpoint:
            raise ExecutorUnavailable("no role executor endpoint configured")
        if workspace is not None:
            # Isolation is checked on our side before anything is sent.
            workspace.require_isolated()
        payload = self._payload(task, model=model, provider=provider, workspace=workspace)
        request = urllib.request.Request(
            f"{self.endpoint}/invoke", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Vertep-Contract": self.contract_version,
                     **({"X-Lab-Executor-Token": self.token} if self.token else {})},
            method="POST")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ExecutorError(
                f"role executor refused the invocation: HTTP {error.code}") from error
        except TimeoutError as error:
            raise ExecutorUnavailable(
                f"role executor timed out after {self.timeout:g}s") from error
        except urllib.error.URLError as error:
            if isinstance(getattr(error, "reason", None), TimeoutError):
                raise ExecutorUnavailable(
                    f"role executor timed out after {self.timeout:g}s") from error
            raise ExecutorUnavailable(
                f"role executor unreachable: {error}") from error
        except OSError as error:
            raise ExecutorUnavailable(
                f"role executor unreachable: {error}") from error
        return self._verify(raw)

    # -- helpers ----------------------------------------------------------
    def _payload(self, task, *, model: str, provider: str, workspace) -> dict:
        task_dict = task.as_dict() if hasattr(task, "as_dict") else dict(task or {})
        payload: dict = {"contract_version": self.contract_version,
                         "task": task_dict,
                         "role": str(task_dict.get("role", "")),
                         "model": model, "provider": provider}
        if workspace is not None:
            payload["workspace"] = {"root": str(workspace.root),
                                    "branch": str(getattr(workspace, "branch", "")),
                                    "state_dir": str(workspace.state_dir)}
        return payload

    def _verify(self, raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ExecutorError("role executor returned a non-JSON response") from error
        if not isinstance(data, dict):
            raise ExecutorError("role executor returned a non-object response")
        status = str(data.get("status", "ok")).lower()
        if status in {"error", "failed", "denied", "blocked"}:
            raise ExecutorError(
                f"role executor reported {status}: {data.get('error') or data.get('reason') or ''}".strip())
        if "result" not in data:
            raise ExecutorError("role executor response has no result")
        return data


def executor_from_environment(url: str = "", *, enabled: bool | None = None,
                              token: str | None = None,
                              timeout: float = 60.0) -> EndpointRoleExecutor | None:
    """Build the executor from an explicit URL or the laboratory environment.

    Returns ``None`` when no endpoint is configured, so callers can report a
    missing executor instead of inventing one.
    """
    endpoint = str(url or "").strip() or os.getenv("LAB_ROLE_EXECUTOR_URL", "").strip()
    if not endpoint:
        return None
    return EndpointRoleExecutor(endpoint, token=token, timeout=timeout, enabled=enabled)
