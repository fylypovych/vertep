#!/usr/bin/env python3
"""Read-only daemon/runtime probes, supervised by the existing host service manager.

No Docker output, environment, container inspect data or credentials are published.
The observer never installs, updates, restarts or configures anything.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.health_observation import write_observation

# Executed inside the actual ComfyUI runtime, not CORE and not the host Python.
GPU_PROBE = """
import json
result = {'gpu': False, 'cuda': False}
try:
    import torch
    result['gpu'] = bool(torch.cuda.is_available() and torch.cuda.device_count())
    if result['gpu']:
        a = torch.ones((16, 16), device='cuda')
        b = a @ a
        torch.cuda.synchronize()
        result['cuda'] = bool((b == 16).all().item())
except Exception:
    pass
print(json.dumps(result))
"""


def observe(root: Path) -> dict[str, bool]:
    checks = {"docker": False, "gpu": False, "cuda": False}
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        checks["docker"] = True
        # The applied plan owns local roles. No GPU access is requested for a
        # remote GPU node or an installation that has not selected GPU.
        plan = json.loads((root / "config/deployment-plan.json").read_text(encoding="utf-8"))
        if "gpu" in {plan.get("role"), *plan.get("additional_roles", [])}:
            import importlib.util
            spec = importlib.util.spec_from_file_location("host_watchdog", Path(__file__).with_name("watchdog.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = subprocess.run([*module.compose_command(root), "exec", "-T", "comfyui", "python", "-c", GPU_PROBE],
                                    check=True, capture_output=True, text=True, timeout=30)
            gpu = json.loads(result.stdout)
            for name in ("gpu", "cuda"):
                checks[name] = gpu.get(name) is True
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        pass
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.getenv("VERTEP_ROOT", "/opt/vertep")))
    parser.add_argument("--loop", action="store_true", help="Run under an external supervisor without systemd")
    args = parser.parse_args()
    while True:
        checks = observe(args.root)
        write_observation(args.root / "config/host-health/observation.json", checks)
        if not args.loop:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
