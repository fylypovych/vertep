import os
import time
import base64
import subprocess
from concurrent.futures import ThreadPoolExecutor
import httpx
from adapters.comfyui import ComfyUIAdapter
from core.gpu_profiles import gpu_profile
from core.logging_config import configure_logging

logger = configure_logging("worker")
pending_logs: list[dict] = []

def submit_result(client: httpx.Client, core: str, result: dict) -> None:
    response = client.post(f"{core}/api/tasks/result", json=result, timeout=90)
    if response.status_code == 409:
        return
    if response.status_code in {400, 413, 422} and result.get("success"):
        rejection = {"job_id": result["job_id"], "task_id": result["task_id"],
                     "node_name": result["node_name"], "success": False,
                     "error": f"CORE rejected generated artifacts: {response.text[:500]}"}
        failed = client.post(f"{core}/api/tasks/result", json=rejection, timeout=90)
        if failed.status_code != 409:
            failed.raise_for_status()
        return
    response.raise_for_status()

def gpu_info() -> dict:
    info = {"gpu_name": os.getenv("GPU_NAME", "unknown"),
            "gpu_count": int(os.getenv("GPU_COUNT", "1")),
            "vram_mb": int(os.getenv("VRAM_MB", "0")),
            "cuda_version": os.getenv("CUDA_VERSION", "unknown"),
            "gpu_available": False}
    try:
        query = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,temperature.gpu,utilization.gpu,driver_version,compute_cap",
                                "--format=csv,noheader,nounits"], check=True, capture_output=True,
                               text=True, timeout=10).stdout.strip().splitlines()
        rows = [[part.strip() for part in line.split(",")] for line in query if line.strip()]
        if rows:
            profile = gpu_profile(rows[0][0], int(rows[0][1]), rows[0][6])
            info.update({"gpu_name": rows[0][0], "gpu_count": len(rows),
                         "vram_mb": sum(int(row[1]) for row in rows),
                         "free_vram_mb": sum(int(row[2]) for row in rows),
                         "temperature": max(float(row[3]) for row in rows),
                         "gpu_load": max(float(row[4]) for row in rows), "driver_version": rows[0][5],
                         "compute_capability": rows[0][6], "gpu_architecture": profile["architecture"],
                         "gpu_profile": profile["profile_id"], "gpu_available": True})
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("nvidia-smi metrics unavailable")
    return info

def worker_status(metrics: dict, require_gpu: bool, busy: bool = False) -> str:
    if require_gpu and not metrics.get("gpu_available", False):
        return "ERROR"
    return "BUSY" if busy else "ONLINE"

def execute_task(adapter: ComfyUIAdapter, task: dict, node_name: str) -> dict:
    try:
        scenes = (task.get("script") or {}).get("scenes") or [{"prompt": task["topic"]}]
        artifacts = []
        for index, scene in enumerate(scenes, 1):
            task_type = task.get("task", "image")
            if hasattr(adapter, "generate_output"):
                data, filename, kind = adapter.generate_output(task["workflow"],
                                                               scene.get("prompt") or task["topic"], task_type)
            else:
                data, filename = adapter.generate(task["workflow"], scene.get("prompt") or task["topic"])
                kind = "image"
            suffix = os.path.splitext(filename)[1] or ".png"
            scene_name = task.get("scene_id") if len(scenes) == 1 and task.get("scene_id") else f"scene-{index:03d}"
            artifacts.append({"filename": f"{scene_name}{suffix}", "kind": kind,
                              "data_base64": base64.b64encode(data).decode("ascii")})
        images = [{"filename": item["filename"], "image_base64": item["data_base64"]}
                  for item in artifacts if item["kind"] == "image"]
        return {"job_id": task["job_id"], "task_id": task["task_id"], "node_name": node_name, "success": True,
                "filename": artifacts[0]["filename"],
                "image_base64": images[0]["image_base64"] if images else None,
                "images": images, "artifacts": artifacts}
    except Exception as error:
        logger.exception("Worker task failed", extra={"job_id": task.get("job_id"), "node_name": node_name})
        pending_logs.append({"level": "ERROR", "message": str(error), "job_id": task.get("job_id")})
        return {"job_id": task["job_id"], "task_id": task["task_id"], "node_name": node_name, "success": False, "error": str(error)}

def main() -> None:
    core = os.getenv("CORE_ADDRESS", "http://localhost:8080")
    supported_tasks = [item.strip() for item in os.getenv("SUPPORTED_TASKS", "image").split(",") if item.strip()]
    supported_workflows = [item.strip() for item in os.getenv("SUPPORTED_WORKFLOWS", "*").split(",") if item.strip()]
    metrics = gpu_info()
    require_gpu = os.getenv("WORKER_REQUIRE_GPU", "true").lower() == "true" and os.getenv("DEMO_MODE", "true").lower() != "true"
    payload = {"node_name": os.getenv("NODE_NAME", "gpu-01"), **metrics,
               "status": worker_status(metrics, require_gpu),
               "supported_tasks": supported_tasks, "supported_workflows": supported_workflows}
    adapter = ComfyUIAdapter()
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    active_task = None
    headers = {"X-Vertep-Token": os.getenv("NODE_API_TOKEN", "")} if os.getenv("NODE_API_TOKEN") else {}
    with httpx.Client(timeout=5, headers=headers) as client:
        while True:
            try:
                metrics = gpu_info()
                payload.update(metrics)
                if future is None and worker_status(metrics, require_gpu) == "ERROR":
                    payload.update({"status": "ERROR", "current_job": None, "current_task": None})
                    client.post(f"{core}/api/workers/heartbeat", json=payload).raise_for_status()
                    time.sleep(15)
                    continue
                if future is None:
                    payload["status"] = worker_status(metrics, require_gpu)
                if future and future.done():
                    result = future.result()
                    submit_result(client, core, result)
                    payload.update({"current_job": None, "status": "ONLINE"})
                    payload["current_task"] = None
                    future = None
                    active_task = None
                if future is None:
                    task = client.post(f"{core}/api/tasks/claim", json={"node_name": payload["node_name"],
                                                                         "gpu_name": payload["gpu_name"],
                                                                         "vram_mb": payload["vram_mb"],
                                                                         "free_vram_mb": payload.get("free_vram_mb"),
                                                                         "supported_tasks": supported_tasks,
                                                                         "supported_workflows": supported_workflows}).json().get("task")
                    if task:
                        logger.info("Task claimed", extra={"job_id": task["job_id"], "node_name": payload["node_name"]})
                        payload.update({"current_job": task["job_id"], "current_task": task["task_id"], "status": "BUSY"})
                        active_task = task
                        future = pool.submit(execute_task, adapter, task, payload["node_name"])
                if future is not None and active_task:
                    client.post(f"{core}/api/tasks/renew", json={"node_name": payload["node_name"],
                                                                  "task_id": active_task["task_id"]}).raise_for_status()
                    cancellations = client.get(f"{core}/api/tasks/cancellations/{payload['node_name']}").json()
                    if any(item["task_id"] == active_task["task_id"] for item in cancellations):
                        adapter.cancel()
                client.post(f"{core}/api/workers/heartbeat", json=payload)
                if pending_logs:
                    response = client.post(f"{core}/api/logs/ingest", json={"node_name": payload["node_name"],
                                                                            "entries": pending_logs[:]})
                    response.raise_for_status()
                    pending_logs.clear()
            except httpx.HTTPError:
                logger.warning("CORE request failed", extra={"node_name": payload["node_name"]})
            except Exception as error:
                payload["status"] = "ERROR"
                pending_logs.append({"level": "ERROR", "message": str(error),
                                     "job_id": payload.get("current_job")})
                logger.exception("Worker loop failed", extra={"node_name": payload["node_name"]})
                try:
                    client.post(f"{core}/api/workers/heartbeat", json=payload)
                except httpx.HTTPError:
                    pass
            time.sleep(15)

if __name__ == "__main__":
    main()
