"""CORE wiring of the module lifecycle (core/module/lifecycle.py).

* package verification - the CORE trust root (signature, issuer, provenance);
* data - ``VERTEP_MODULE_DATABASE_URL`` (or ``DATABASE_URL``); the role needs
  CREATEROLE to provision per-module roles/schemas;
* self-test - a Worker on a placed node reports the new version ready;
* drain - running workflow tasks of the module finish or are revoked;
* database credentials - stored in the encrypted platform secret store;
* startup - ``recover()`` finishes or undoes every unfinished operation.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .lifecycle import ModuleLifecycle

_lock = threading.Lock()
DATABASE_SECRET = "__database__"


def lifecycle_root() -> Path:
    configured = os.getenv("VERTEP_MODULE_LIFECYCLE_ROOT")
    if configured:
        return Path(configured)
    from core.first_run import config_root
    return config_root() / "module-lifecycle"


def _trust():
    from .trust import TrustStore
    project = Path(__file__).resolve().parents[2]
    return TrustStore.from_root(Path(os.getenv("VERTEP_MODULE_TRUST_ROOT", str(project / "config/module-trust"))))


def store_credentials(module_id: str, credentials: dict) -> None:
    from core.first_run import _secret_lock, _write_encrypted_secrets, ensure_secret_store
    from .gateway import secret_key
    with _secret_lock:
        stored = ensure_secret_store()
        stored[secret_key(module_id, DATABASE_SECRET)] = json.dumps(credentials)
        _write_encrypted_secrets(stored)


def _notify_administrator(message: str, details: dict) -> None:
    """#513: a stopped automatic update becomes a CORE alert the administrator sees."""
    try:
        from core.alert_store import get_alert_store
        get_alert_store().record({"severity": "critical", "type": "MODULE_UPDATE_STOPPED", "message": message,
                                  "details": details, "operation_id": details.get("operation")})
    except Exception:  # noqa: BLE001 - notification never breaks recovery
        pass


def attach(runtime) -> ModuleLifecycle:
    """Create the lifecycle for a WorkflowRuntime and recover unfinished operations."""
    from .data import ModuleDatabase
    from .packages import verify_package
    dsn = os.getenv("VERTEP_MODULE_DATABASE_URL") or os.getenv("DATABASE_URL")
    registry = runtime.modules

    def self_test(manifest):
        deadline = time.monotonic() + manifest.health["timeout_seconds"]
        while time.monotonic() < deadline:
            for instance in registry.instances(manifest.module_id):
                others = [r for r in instance.reasons if not r.startswith("lifecycle_")]
                if instance.manifest.version == manifest.version and instance.reported_ready and not others:
                    return True
            time.sleep(0.5)
        return False

    def running(module_id):
        engine = runtime.engine
        with engine._lock:
            result = []
            for job in engine._jobs.values():
                definitions = {task["id"]: task for task in job.get("definition", {}).get("tasks", [])}
                for task_id, task in job.get("tasks", {}).items():
                    if task.get("status") == "RUNNING" and definitions.get(task_id, {}).get("module") == module_id:
                        result.append(task.get("dispatch_id"))
            return result

    def cancel(module_id):
        for dispatch_id in running(module_id):
            if dispatch_id:
                runtime.dispatcher.revoke_task(dispatch_id)

    lifecycle = ModuleLifecycle(lifecycle_root(), lambda package: verify_package(package, _trust()),
                                database=ModuleDatabase(dsn) if dsn else None, registry=registry,
                                self_test=self_test, active_tasks=running, cancel_tasks=cancel,
                                store_credentials=store_credentials, system_versions=registry.system_versions,
                                drain_timeout=float(os.getenv("VERTEP_MODULE_DRAIN_SECONDS", "60")),
                                notify=_notify_administrator)
    recovered = lifecycle.recover()
    if recovered:
        from .gateway import audit
        for item in recovered:
            audit("module.lifecycle.recover", item["outcome"], module=item["module_id"], op=item["op"])
    return lifecycle
