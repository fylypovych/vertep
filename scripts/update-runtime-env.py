#!/usr/bin/env python3
"""Atomically switch managed image references while preserving appliance secrets."""

import argparse
import json
import os
import stat
from pathlib import Path


IMAGE_KEYS = {
    "proxy": "VERTEP_PROXY_IMAGE",
    "core": "VERTEP_CORE_IMAGE",
    "license-manager": "VERTEP_LICENSE_MANAGER_IMAGE",
    "dispatcher": "VERTEP_DISPATCHER_IMAGE",
    "scheduler": "VERTEP_SCHEDULER_IMAGE",
    "certificate-manager": "VERTEP_CERTIFICATE_MANAGER_IMAGE",
    "worker": "VERTEP_WORKER_IMAGE",
    "tts": "VERTEP_TTS_IMAGE",
    "publisher-worker": "VERTEP_PUBLISHER_WORKER_IMAGE",
    "backup-service": "VERTEP_BACKUP_SERVICE_IMAGE",
    "postgres": "VERTEP_POSTGRES_IMAGE",
    "redis": "VERTEP_REDIS_IMAGE",
    "ollama": "VERTEP_OLLAMA_IMAGE",
    "monitoring": "VERTEP_MONITORING_IMAGE",
    "grafana": "VERTEP_GRAFANA_IMAGE",
    "log-store": "VERTEP_LOG_STORE_IMAGE",
    "log-collector": "VERTEP_LOG_COLLECTOR_IMAGE",
    "update-agent": "VERTEP_UPDATE_AGENT_IMAGE",
}


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return lines, values


def update(root_env: Path, manifest_path: Path, version: str) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        raise ValueError("release manifest version mismatch")
    images = manifest.get("images")
    if not isinstance(images, dict):
        raise ValueError("release manifest has no image inventory")
    lines, existing = read_env(root_env)
    updates = {"VERTEP_VERSION": version,
               "VERTEP_UPDATE_SERVER":
                   "https://api.github.com/repos/fylypovych/vertep/releases"}
    mapping = dict(IMAGE_KEYS)
    gpu_service = "comfyui-amd" if existing.get("GPU_VENDOR") == "amd" else "comfyui"
    mapping[gpu_service] = "VERTEP_COMFYUI_IMAGE"
    for service, key in mapping.items():
        image = images.get(service)
        if image is None:
            if service == gpu_service:
                raise ValueError(f"release has no image for selected GPU service {service}")
            continue
        reference, digest = image.get("reference"), image.get("digest")
        if not isinstance(reference, str) or not isinstance(digest, str):
            raise ValueError(f"invalid signed image metadata for {service}")
        updates[key] = f"{reference}@{digest}"
    output, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
    mode = stat.S_IMODE(root_env.stat().st_mode)
    temporary = root_env.with_name(f".{root_env.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(root_env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("env", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()
    update(args.env, args.manifest, args.version)


if __name__ == "__main__":
    main()
