"""Execution ownership for modules bound to the shared production executors."""

from time import monotonic
import json
import os
from pathlib import Path


def execute_bound(module, task):
    task_id = str(task.get("task_id") or task.get("job_id") or "")
    if not task_id:
        raise ValueError("Module execution requires task identity")
    module._active_tasks[task_id] = {"started": monotonic()}
    try:
        result = module._bound_executor(task)
        from worker.migration_trace import replaying
        if os.getenv("MODULE_STATE_ROOT") and not replaying():
            path = Path(os.environ["MODULE_STATE_ROOT"]) / module.name / "state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"schema_version": "module-state/1", "last_task": task_id,
                                             "last_output": result}), encoding="utf-8")
            temporary.replace(path)
        return result
    finally:
        module._active_tasks.pop(task_id, None)
