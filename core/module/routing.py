"""Route switch of the existing role pipeline to lifecycle-managed modules (#497).

The legacy Job pipeline keeps its queue, Job states, leases and executors. Only
the choice of the executing node and the execution path changes:

* function not managed by the module lifecycle, or only INSTALLED -> legacy
  role path (unchanged);
* managed and ACTIVE -> only nodes with a READY placed instance may claim, and
  the task carries ``module_name``/``module_version``/``module_routed`` so the
  Worker runs the same executor through the module path;
* managed but not ACTIVE (deactivated, updating, failed) -> no node may claim:
  the task waits in the queue instead of silently falling back.

Uninstalling the module returns the function to the legacy role path.
"""

from __future__ import annotations

TASK_MODULES = {"text": "text", "script": "text", "storyboard": "text",
                "image": "image", "video": "image",
                "voice": "audio", "publish": "publisher", "backup": "backup"}
BLOCKED = "blocked"


def _runtime():
    from core.workflow.runtime import workflow_runtime
    runtime = workflow_runtime()
    return runtime if runtime is not None and getattr(runtime, "lifecycle", None) is not None else None


def task_kind(task: dict) -> str | None:
    if task.get("kind") == "image_storyboard":
        return "image"
    return task.get("task")


def binding(task_type: str | None, node_name: str):
    """None (legacy), BLOCKED, or the module binding for this node."""
    module_id = TASK_MODULES.get(task_type or "")
    runtime = _runtime()
    if module_id is None or runtime is None:
        return None
    record = runtime.lifecycle.record(module_id)
    if record is None or record["state"] == "INSTALLED":
        return None  # not switched yet: installation alone does not take the function over
    if record["state"] != "ACTIVE":
        return BLOCKED
    for instance in runtime.modules.ready_instances(module_id):
        if instance.node_id == node_name:
            return {"module_name": module_id, "module_version": instance.manifest.version, "module_routed": True}
    return BLOCKED


def eligible(workers: list[dict], task_type: str | None) -> list[dict]:
    return [worker for worker in workers if binding(task_type, worker.get("node_name", "")) != BLOCKED]
