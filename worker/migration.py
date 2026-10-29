"""Single-owner role/module switch, provider-replayed shadow parity and telemetry."""

from copy import deepcopy
from importlib import import_module
import hashlib
import json
import os
from threading import RLock
from time import monotonic

from .migration_trace import recording

_lock = RLock()
_active = set()
_modules = {}
_metrics = {}
ROUTES = {"text": ("text", "TextModule", {"text", "script", "storyboard"}),
          "gpu": ("image", "ImageModule", {"image", "video"}),
          "voice": ("audio", "AudioModule", {"voice"})}


def enabled(name, suffix="ENABLED"):
    return os.getenv(f"VERTEP_{name.upper()}_MODULE_{suffix}", "false").lower() in {"true", "1", "yes"}


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items() if key not in {"generated_at", "created_at", "execution_time"}}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(normalized(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def execute(role, task, legacy):
    name, class_name, kinds = ROUTES[role]
    if task.get("task", role) not in kinds:
        return legacy(role, task, _migration=True)
    key = (str(task.get("task_id", "")), str(task.get("lease_id", "")))
    with _lock:
        if key[0] and key in _active:
            raise RuntimeError("Task already has an execution owner")
        _active.add(key)
        module = _modules.get(role)
        if module is None:
            cls = getattr(import_module(f"modules.{name}.{name}_module"), class_name)
            module = cls(executor=lambda value: legacy(role, value, _migration=True))
            _modules[role] = module
    started = monotonic()
    selected = "module" if enabled(name) else "role"
    record = {"task_id": key[0], "lease_id": key[1], "owner": os.getenv("NODE_NAME", role), "path": selected}
    try:
        if enabled(name, "SHADOW"):
            trace = []
            with recording(trace):
                primary = legacy(role, deepcopy(task), _migration=True)
            with recording(trace, replay=True):
                shadow = module.execute(deepcopy(task))
            record.update(path="shadow", parity=digest(primary) == digest(shadow),
                          role_sha256=digest(primary), module_sha256=digest(shadow), external_effects=len(trace))
            result = primary
        else:
            result = module.execute(task) if selected == "module" else legacy(role, task, _migration=True)
        record.update(success=True, output_sha256=digest(result))
        return result
    except Exception as exc:
        record.update(success=False, error=type(exc).__name__)
        raise
    finally:
        record["latency_seconds"] = monotonic() - started
        with _lock:
            _active.discard(key)
            metric = _metrics.setdefault(name, {"executions": 0, "successes": 0, "failures": 0, "latency_seconds": 0.0})
            metric["executions"] += 1
            metric["successes" if record.get("success") else "failures"] += 1
            metric["latency_seconds"] += record["latency_seconds"]
            metric["last_execution"] = record


def overview():
    with _lock:
        return deepcopy(_metrics)


def inventory():
    result = []
    for role, (name, _, _) in ROUTES.items():
        if os.getenv("NODE_ROLE") and os.environ["NODE_ROLE"] != role:
            continue
        if not (enabled(name) or enabled(name, "SHADOW")):
            continue
        from pathlib import Path
        import yaml
        manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "modules" / name / "manifest.yaml").read_text(encoding="utf-8"))
        data = manifest.get("manifest", manifest)
        resources = data.get("resources", {})
        result.append({**data, "dependencies": data.get("depends_on", []), "resource_requirements": {
            "min_vram_mb": resources.get("min_vram_mb", 0), "min_cpu_cores": resources.get("cpu_cores", 0), "min_ram_mb": resources.get("ram_mb", 0)},
            "ready": True, "origin": "bundled", "metrics": overview().get(name, {})})
    return result
