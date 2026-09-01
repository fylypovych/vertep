#!/usr/bin/env python3
"""Build the signed, deterministic appliance payload consumed by bootstrap.sh."""

import argparse
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.release_contract import sign_release_contract


def _load_script(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py").replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load release helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = _load_script("generate-sbom.py").generate
build_contract = _load_script("runtime-contract.py").build_contract


FILES = {
    "deploy/docker-compose.yml": "docker-compose.yml",
    "deploy/docker-compose.amd.yml": "docker-compose.amd.yml",
    "deploy/docker-compose.nvidia.yml": "docker-compose.nvidia.yml",
    "deploy/proxy.conf": "runtime/proxy.conf",
    "config/node_roles.json": "config/node_roles.json",
    "core/deployment_plan.py": "runtime/deployment-plan.py",
    "VERSION": "VERSION",
}

SCRIPTS = (
    "apply-deployment.py", "migrate.py", "release-layout.py", "safe-extract.py",
    "startup-recovery.py", "status.py", "update-agent.py", "vertep", "watchdog.py",
    "worker_update.py", "update-runtime-env.py",
)

UNITS = (
    "vertep-update.service", "vertep-update.path", "vertep-update-check.service",
    "vertep-update.timer", "vertep-deployment.service", "vertep-deployment.path",
    "vertep-watchdog.service", "vertep-watchdog.timer",
    "vertep-startup-recovery.service", "vertep-worker-update.service",
    "vertep-worker-update.path",
)

MONITORING = (
    "monitoring/prometheus.yml", "monitoring/alerts.yml", "monitoring/loki.yml",
    "monitoring/promtail.yml", "monitoring/grafana/provisioning/datasources/vertep.yml",
    "monitoring/grafana/provisioning/dashboards/vertep.yml",
    "monitoring/grafana/dashboards/fleet.json",
)


def _copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def build(root: Path, output: Path, version: str, sequence: int,
          image_lock: Path, private_key: Path) -> Path:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    for source, destination in FILES.items():
        _copy(root / source, output / destination)
    for name in SCRIPTS:
        _copy(root / "scripts" / name, output / "scripts" / name)
    for name in UNITS:
        _copy(root / "installer" / name, output / name)
    for name in MONITORING:
        _copy(root / name, output / name)
    _copy(root / "installer/update-public.pem", output / "installer/update-public.pem")
    shutil.copytree(root / "core", output / "core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(root / "db", output / "db",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    images = json.loads(image_lock.read_text(encoding="utf-8"))
    (output / "images.json").write_text(
        json.dumps(images, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sbom = generate(root, image_lock, version)
    (output / "sbom.cdx.json").write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    contract = build_contract(
        output, version, sequence, "stable", "config/node_roles.json",
        output / "images.json", "sbom.cdx.json", datetime.now(timezone.utc), 365,
        {"core_api": 1, "worker_api": 1, "database_schema": 9,
         "database_strategy": "expand", "rollback_safe": True,
         "minimum_version": "0.0.0.1"},
    )
    signed = sign_release_contract(contract, private_key)
    (output / "manifest.json").write_text(
        json.dumps(signed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--image-lock", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.root.resolve(), args.output.resolve(), args.version, args.sequence,
                args.image_lock.resolve(), args.private_key.resolve()))


if __name__ == "__main__":
    main()
