import json
import subprocess
import sys
from pathlib import Path

config = {}
path = Path("/etc/vertep/node.conf")
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            config[key] = value
try:
    status = json.load(sys.stdin)
except ValueError:
    status = {}
role = config.get("ROLE", "unknown").upper()
print(f"VERTEP {role}\nNode: {config.get('NODE_NAME', 'unknown')}")
for name in ("core", "postgres", "redis", "ollama", "telegram"):
    if name in status:
        print(f"{name.title():14} {status[name]}")
queue = status.get("queue", {})
if queue:
    print(f"Queue          ready={queue.get('depth', 0)} inflight={queue.get('inflight', 0)} dead={queue.get('dead_letter', 0)}")
scheduler = status.get("scheduler", {})
if scheduler:
    print(f"Scheduler      pending={scheduler.get('pending', 0)} next={scheduler.get('next_run') or '-'}")
orchestration = status.get("orchestration", {})
if orchestration:
    print(f"Orchestration  jobs={orchestration.get('active_jobs', 0)} scenes={orchestration.get('active_scenes', 0)}")
for worker in status.get("workers", []):
    print(f"{worker.get('node_name','worker'):14} {worker.get('status')} {worker.get('gpu_name')} {worker.get('free_vram_mb', worker.get('vram_mb', 0))} MB")
if "worker" in role.lower():
    try:
        print(subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,utilization.gpu,temperature.gpu",
                             "--format=csv,noheader"], capture_output=True, text=True, timeout=5).stdout.strip())
    except OSError:
        print("NVIDIA GPU     unavailable")
