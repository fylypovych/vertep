"""Disposable one-task client using production Worker execution/result owners."""

import json
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker.service import execute_task, submit_result


with httpx.Client(headers={"X-Vertep-Token": os.environ["NODE_API_TOKEN"]}, timeout=20) as client:
    core = sys.argv[1]
    response = client.post(core + "/api/tasks/claim/workflow", json={"node_name": "text-process", "protocol_version": "1.0.0"})
    response.raise_for_status()
    task = response.json()["task"]
    if task is None:
        raise RuntimeError("Workflow task was not dispatched")
    result = execute_task(None, task, "text-process")
    result.update(architecture_workflow=True, lease_id=task["lease_id"])
    submit_result(client, core, result)
    print(json.dumps({"task_id": task["task_id"], "lease_id": task["lease_id"], "success": result["success"],
                      "dependencies": list(task.get("dependencies", {}))}))
