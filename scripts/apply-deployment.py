#!/usr/bin/env python3
"""Застосування вибраної у Web Wizard ролі на appliance-хості."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.deployment_plan import create_plan


BOOTSTRAP_SERVICES = {"proxy", "core", "license-manager", "dispatcher", "scheduler",
                      "certificate-manager", "migrate", "postgres", "redis", "update-agent"}
SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
SAFE_MODEL = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}")


def env_value(value: object, name: str) -> str:
    if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
        raise ValueError(f"Некоректне значення {name}")
    return value


def current_version(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERTEP_VERSION="):
            return line.split("=", 1)[1]
    raise ValueError("У .env відсутня встановлена версія Vertep")


def environment_value(path: Path, name: str, default: str = "") -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return default


def update_env(path: Path, values: dict[str, str]) -> None:
    values = {key: env_value(value, key) for key, value in values.items()}
    rows, seen = [], set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
            if key in values:
                rows.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                rows.append(line)
    rows.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


def runtime_inventory(root: Path, compose: list[str], selected: set[str], role: str,
                      version: str, runner=subprocess.run) -> dict:
    result = runner([*compose, "ps", "--format", "json"], check=True, timeout=120,
                    capture_output=True, text=True)
    rows = []
    output = getattr(result, "stdout", "") or ""
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("Service") in selected:
            digest = None
            container_id = item.get("ID")
            if container_id:
                inspected = runner(["docker", "inspect", "-f", "{{.Image}}", container_id],
                                   check=True, timeout=30, capture_output=True, text=True)
                digest = (getattr(inspected, "stdout", "") or "").strip() or None
            rows.append({"service": item.get("Service"), "image": item.get("Image"),
                         "image_digest": digest, "state": item.get("State"),
                         "health": item.get("Health") or ""})
    inventory = {"schema": 1, "version": version, "role": role,
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "services": sorted(selected), "containers": rows}
    atomic_json(root / "config/runtime-inventory.json", inventory)
    return inventory


def wait_for_healthy(compose: list[str], selected: set[str], runner=subprocess.run,
                     timeout_seconds: int = 600) -> None:
    deadline = time.monotonic() + timeout_seconds
    required_running = selected - {"migrate"}
    while True:
        result = runner([*compose, "ps", "--format", "json"], check=True, timeout=120,
                        capture_output=True, text=True)
        output = getattr(result, "stdout", "") or ""
        rows = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("Service") in selected:
                rows.append(item)
        # Unit-test runners and old Compose clients may not expose JSON output.
        if not rows:
            return
        ready = {item.get("Service") for item in rows
                 if ((item.get("State") == "running" and (item.get("Health") or "healthy") == "healthy")
                     or (item.get("Service") == "migrate" and item.get("State") == "exited"))}
        failed = [item.get("Service") for item in rows
                  if item.get("Health") == "unhealthy"
                  or (item.get("State") == "exited" and item.get("Service") != "migrate")]
        if failed:
            raise RuntimeError("Selected services failed health checks: " + ", ".join(sorted(failed)))
        if required_running <= ready:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Selected services did not become healthy before timeout")
        time.sleep(5)


def apply(root: Path, runner=subprocess.run) -> dict:
    request_path = root / "config/deployment-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    roles = json.loads((root / "config/node_roles.json").read_text(encoding="utf-8"))
    role = request.get("role")
    version = request.get("version")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise ValueError("Deployment request містить некоректну версію")
    if version != current_version(root / ".env"):
        raise RuntimeError("Deployment request створено для іншої версії Vertep")
    plan = create_plan(roles, role, version)
    if request.get("plan_sha256") != plan["sha256"]:
        raise RuntimeError("Deployment request не відповідає підписаному каталогу ролей")
    core_url = request.get("core_url") or ""
    if role != "core":
        core_url = env_value(core_url, "CORE_URL").rstrip("/")
        parsed = urlsplit(core_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Non-Core роль потребує коректний HTTPS Core URL")
    model = request.get("ollama_model", "llama3.2")
    if role == "text" and (not isinstance(model, str) or not SAFE_MODEL.fullmatch(model)):
        raise ValueError("Некоректна назва Ollama model")
    update_env(root / ".env", {
        "NODE_ROLE": role,
        "NODE_CAPABILITIES": ",".join(plan["capabilities"]),
        "CORE_URL": core_url,
        "CORE_ADDRESS": core_url,
        "REGISTRATION_TOKEN": "",
    })
    atomic_json(root / "config/deployment-plan.json", plan)
    compose = ["docker", "compose", "--env-file", str(root / ".env"),
               "-f", str(root / "docker-compose.yml")]
    if environment_value(root / ".env", "GPU_VENDOR") == "amd" and (root / "docker-compose.amd.yml").is_file():
        compose.extend(["-f", str(root / "docker-compose.amd.yml")])
    if environment_value(root / ".env", "GPU_VENDOR") == "nvidia" and (root / "docker-compose.nvidia.yml").is_file():
        compose.extend(["-f", str(root / "docker-compose.nvidia.yml")])
    selected = set(plan["services"])
    all_services = BOOTSTRAP_SERVICES | {service for definition in roles.values()
                                        for service in definition["services"]}
    unwanted = sorted(all_services - selected)
    status = {"state": "APPLYING", "role": role, "services": sorted(selected),
              "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(root / "config/deployment-status.json", status)
    try:
        runner([*compose, "pull", *sorted(selected)], check=True, timeout=3600)
        runner([*compose, "up", "-d", "--remove-orphans", *sorted(selected)],
               check=True, timeout=1800)
        if role == "text":
            runner([*compose, "exec", "-T", "ollama", "ollama", "pull", model],
                   check=True, timeout=3600)
        wait_for_healthy(compose, selected, runner)
        inventory = runtime_inventory(root, compose, selected, role, version, runner)
        unhealthy = [item["service"] for item in inventory["containers"]
                     if ((item["state"] != "running" and item["service"] != "migrate")
                         or item["health"] not in {"", "healthy"})]
        if unhealthy:
            raise RuntimeError("Selected services are not healthy: " + ", ".join(unhealthy))
        inventory["modules"] = {name: "HEALTHY" for name in plan["modules"]}
        atomic_json(root / "config/runtime-inventory.json", inventory)
        installation_path = root / "config/installation.json"
        if installation_path.is_file():
            installation = json.loads(installation_path.read_text(encoding="utf-8"))
            installation["runtime"] = inventory
            atomic_json(installation_path, installation)
            public_manifest = {key: value for key, value in installation.items()
                               if key != "administrator"}
            public_manifest["administrator"] = {
                "username": installation.get("administrator", {}).get("username"), "role": "admin"}
            public_manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
                {key: value for key, value in installation.items() if key != "administrator"},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            atomic_json(root / "config/installation-manifest.json", public_manifest)
        if unwanted:
            runner([*compose, "stop", *unwanted], check=True, timeout=600)
            runner([*compose, "rm", "-f", *unwanted], check=True, timeout=600)
        status.update({"state": "SUCCEEDED", "updated_at": datetime.now(timezone.utc).isoformat()})
        atomic_json(root / "config/deployment-status.json", status)
        request_path.unlink()
        return status
    except Exception as error:
        status.update({"state": "FAILED", "error": str(error),
                       "updated_at": datetime.now(timezone.utc).isoformat()})
        atomic_json(root / "config/deployment-status.json", status)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Застосувати роль Vertep із Web Wizard")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    apply(args.root.resolve())


if __name__ == "__main__":
    main()
