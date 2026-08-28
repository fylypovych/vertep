import os
import time
import base64
import subprocess
import json
import shutil
import tempfile
import platform
import secrets
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from adapters.comfyui import ComfyUIAdapter
from core.gpu_profiles import gpu_profile
from core.logging_config import configure_logging
from worker.role_executor import execute_role_task

logger = configure_logging("worker")
pending_logs: list[dict] = []

def role_self_test(role: str, metrics: dict, adapter: ComfyUIAdapter | None = None) -> dict:
    started = time.monotonic()
    try:
        if role == "gpu":
            if os.getenv("DEMO_MODE", "true").lower() != "true" and not metrics.get("gpu_available"):
                raise RuntimeError("NVIDIA GPU/driver is unavailable")
            adapter = adapter or ComfyUIAdapter()
            data, _, kind = adapter.generate_output(os.getenv("SELF_TEST_WORKFLOW", "workflows/image/demo.json"),
                                                     "Vertep worker self-test", "image")
            if kind != "image" or len(data) < 16:
                raise RuntimeError("GPU workflow returned no image")
        elif role == "text":
            response = httpx.post(f"{os.getenv('OLLAMA_URL', 'http://ollama:11434')}/api/generate",
                                  json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"),
                                        "prompt": "Reply OK", "stream": False}, timeout=60)
            response.raise_for_status()
            if not response.json().get("response"):
                raise RuntimeError("Ollama returned no text")
        elif role == "voice":
            url = os.getenv("TTS_HEALTH_URL")
            if not url or httpx.get(url, timeout=15).status_code >= 400:
                raise RuntimeError("TTS runtime health check failed")
        elif role == "publisher":
            if not os.getenv("PUBLISHER_HEALTH_URL"):
                raise RuntimeError("Publisher runtime is not configured")
            httpx.get(os.environ["PUBLISHER_HEALTH_URL"], timeout=15).raise_for_status()
        elif role == "monitoring":
            httpx.get(os.getenv("PROMETHEUS_URL", "http://monitoring:9090/-/healthy"), timeout=15).raise_for_status()
        elif role == "backup":
            root = Path(os.getenv("BACKUP_ROOT", "/data/backups"))
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
                output.write(b"vertep-backup-self-test")
                output.flush()
                os.fsync(output.fileno())
                path = Path(output.name)
            if path.read_bytes() != b"vertep-backup-self-test":
                raise RuntimeError("Backup storage read-after-write failed")
            path.unlink()
        else:
            raise RuntimeError(f"No self-test is implemented for role {role}")
        return {"status": "PASSED", "role": role, "checked_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except Exception as error:
        return {"status": "FAILED", "role": role, "checked_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000), "error": str(error)[:500]}

def configured_role() -> str:
    role = os.getenv("NODE_ROLE", "gpu")
    if role != "unassigned":
        return role
    try:
        return str(json.loads((Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json")).parent
                               / "installation.json").read_text())["node_role"])
    except (OSError, ValueError, KeyError):
        return "gpu"

def node_capabilities() -> list[str]:
    defaults = {"gpu": "image_generation,image_upscale,controlnet,inpainting",
                "text": "text_generation", "voice": "speech_synthesis",
                "publisher": "publishing", "backup": "backup,snapshot,archive",
                "monitoring": "metrics,logs,alerting"}
    role = configured_role()
    return sorted({item.strip() for item in os.getenv("NODE_CAPABILITIES", defaults.get(role, "")).split(",")
                   if item.strip()})

def enroll(client: httpx.Client, core: str, node_name: str, metrics: dict,
           capabilities: list[str]) -> str:
    config_path = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    try:
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        if stored.get("jwt"):
            return str(stored["jwt"])
    except (OSError, ValueError):
        pass
    registration_token = os.getenv("REGISTRATION_TOKEN", "")
    if not registration_token:
        return os.getenv("NODE_API_TOKEN", "")
    memory_mb = None
    try:
        memory_mb = int(Path("/proc/meminfo").read_text().split("MemTotal:", 1)[1].split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    pki = config_path.parent / "pki"
    pki.mkdir(parents=True, exist_ok=True)
    key, csr = pki / "node.key", pki / "node.csr"
    if not key.exists():
        subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={node_name}",
                        "-keyout", str(key), "-out", str(csr)], check=True, capture_output=True)
        os.chmod(key, 0o600)
    elif not csr.exists():
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                        "-out", str(csr)], check=True, capture_output=True)
    response = client.post(f"{core}/api/nodes/register", json={"registration_token": registration_token,
        "node_id": node_name, "capabilities": capabilities, "version": os.getenv("VERTEP_VERSION", "unknown"),
        "csr": csr.read_text(encoding="utf-8"),
        "hardware": {**metrics, "ram_mb": memory_mb,
                     "disk_free_mb": shutil.disk_usage("/").free // 1024 // 1024}}, timeout=30)
    response.raise_for_status()
    credentials = response.json()
    (pki / "node.crt").write_text(credentials["certificate"], encoding="utf-8")
    (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    return str(credentials["jwt"])

def renew_if_needed(core: str, node_name: str, credential: str, verify: str | bool,
                    pki: Path, config_path: Path) -> str:
    certificate, key, csr = pki / "node.crt", pki / "node.key", pki / "node.csr"
    if not certificate.is_file() or not key.is_file():
        return credential
    threshold = int(os.getenv("CERTIFICATE_RENEW_BEFORE_SECONDS", str(30 * 86400)))
    valid = subprocess.run(["openssl", "x509", "-checkend", str(threshold), "-noout",
                            "-in", str(certificate)], capture_output=True, check=False).returncode == 0
    if valid:
        return credential
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                    "-out", str(csr)], check=True, capture_output=True)
    try:
        renewal_credential = json.loads(config_path.read_text(encoding="utf-8")).get("worker_secret") or credential
    except (OSError, ValueError):
        renewal_credential = credential
    with httpx.Client(timeout=30, verify=verify, cert=(str(certificate), str(key)),
                      headers={"X-Vertep-Token": renewal_credential}) as client:
        response = client.post(f"{core}/api/nodes/{node_name}/renew",
                               json={"csr": csr.read_text(encoding="utf-8")})
        response.raise_for_status()
    credentials = response.json()
    certificate.write_text(credentials["certificate"], encoding="utf-8")
    (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    return str(credentials["jwt"])

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


def host_metrics() -> dict:
    memory_mb = None
    try:
        values = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        memory_mb = int(next(line for line in values if line.startswith("MemAvailable:"))
                        .split()[1]) // 1024
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    try:
        cpu_load = os.getloadavg()[0]
    except OSError:
        cpu_load = None
    return {"ram_mb": memory_mb, "disk_free_mb": shutil.disk_usage("/").free // 1024 // 1024,
            "cpu_load": cpu_load, "runtime_version": platform.python_version()}


def request_local_update(target_version: str) -> None:
    request_root = os.getenv("UPDATE_REQUEST_DIR", "")
    if not request_root:
        raise RuntimeError("UPDATE_REQUEST_DIR is required for coordinated rolling updates")
    root = Path(request_root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root.parent / "worker-update-target"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == target_version:
        return
    request_id = secrets.token_hex(16)
    temporary = root / f".{request_id}.tmp"
    temporary.write_text(json.dumps({"request_id": request_id, "action": "update",
                                     "target_version": target_version}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(root / f"{request_id}.json")
    marker.write_text(target_version, encoding="utf-8")

def worker_status(metrics: dict, require_gpu: bool, busy: bool = False) -> str:
    if require_gpu and not metrics.get("gpu_available", False):
        return "ERROR"
    return "BUSY" if busy else "READY"

def execute_task(adapter: ComfyUIAdapter, task: dict, node_name: str) -> dict:
    try:
        if task.get("task", "image") in {"text", "voice", "publish", "backup"}:
            artifacts = execute_role_task(configured_role(), task)
            return {"job_id": task["job_id"], "task_id": task["task_id"],
                    "node_name": node_name, "success": True,
                    "filename": artifacts[0]["filename"], "artifacts": artifacts}
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
    capabilities = node_capabilities()
    payload = {"node_name": os.getenv("NODE_NAME", "gpu-01"), **metrics, **host_metrics(),
               "status": worker_status(metrics, require_gpu),
               "supported_tasks": supported_tasks, "supported_workflows": supported_workflows,
               "role": configured_role(), "capabilities": capabilities,
               "version": os.getenv("VERTEP_VERSION")}
    adapter = ComfyUIAdapter()
    self_test = role_self_test(configured_role(), metrics, adapter)
    payload["self_test"] = self_test
    if self_test["status"] != "PASSED":
        payload["status"] = "ERROR"
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    active_task = None
    enrollment_verify = os.getenv("CORE_CA_PATH", "")
    if core.startswith("https://") and os.getenv("REGISTRATION_TOKEN") and not enrollment_verify:
        raise RuntimeError("CORE_CA_PATH is required to pin Core before sending a registration token")
    with httpx.Client(timeout=5, verify=enrollment_verify or True) as enrollment_client:
        credential = enroll(enrollment_client, core, payload["node_name"], metrics, capabilities)
    pki = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json")).parent / "pki"
    config_path = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    credential = renew_if_needed(core, payload["node_name"], credential, enrollment_verify or True,
                                 pki, config_path)
    client_options = {"timeout": 5, "headers": {"X-Vertep-Token": credential} if credential else {}}
    if core.startswith("https://") and (pki / "node.crt").is_file():
        client_options.update({"verify": enrollment_verify or True,
                               "cert": (str(pki / "node.crt"), str(pki / "node.key"))})
    with httpx.Client(**client_options) as client:
        desired_state = None
        next_self_test = time.monotonic() + (15 if self_test["status"] != "PASSED"
                                             else float(os.getenv("SELF_TEST_INTERVAL_SECONDS", "300")))
        while True:
            try:
                metrics = gpu_info()
                payload.update(metrics)
                payload.update(host_metrics())
                if future is None and time.monotonic() >= next_self_test:
                    payload["self_test"] = role_self_test(configured_role(), metrics, adapter)
                    next_self_test = time.monotonic() + (15 if payload["self_test"]["status"] != "PASSED"
                                                         else float(os.getenv("SELF_TEST_INTERVAL_SECONDS", "300")))
                if payload.get("self_test", {}).get("status") != "PASSED":
                    payload.update({"status": "ERROR", "current_job": None, "current_task": None})
                    client.post(f"{core}/api/workers/heartbeat", json=payload).raise_for_status()
                    time.sleep(15)
                    continue
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
                    payload.update({"current_job": None, "status": "READY"})
                    payload["current_task"] = None
                    future = None
                    active_task = None
                if future is None and desired_state not in {"DRAINING", "QUARANTINED"}:
                    task = client.post(f"{core}/api/tasks/claim", json={"node_name": payload["node_name"],
                                                                         "gpu_name": payload["gpu_name"],
                                                                         "vram_mb": payload["vram_mb"],
                                                                         "free_vram_mb": payload.get("free_vram_mb"),
                                                                         "supported_tasks": supported_tasks,
                                                                         "supported_workflows": supported_workflows,
                                                                         "capabilities": capabilities}).json().get("task")
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
                heartbeat_response = client.post(f"{core}/api/workers/heartbeat", json=payload)
                heartbeat_response.raise_for_status()
                control = heartbeat_response.json()
                desired_state = control.get("desired_state")
                update_target = control.get("update_target_version")
                if update_target and future is None:
                    request_local_update(update_target)
                    desired_state = "UPDATING"
                    payload["status"] = "UPDATING"
                if control.get("self_test_requested_at") and future is None:
                    next_self_test = 0
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
