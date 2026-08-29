#!/usr/bin/env python3
"""Застосування вибраної у Web Wizard ролі на appliance-хості."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.deployment_plan import create_plan


BOOTSTRAP_SERVICES = {"proxy", "core", "migrate", "postgres", "redis", "update-agent"}
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
