#!/usr/bin/env python3
"""Generate a signed Vertep release bundle for GitHub Releases."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


VERSION_RE = re.compile(r"^v?(0\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)$")
BUNDLE_FILES = [
    "docker-compose.yml",
    "docker-compose.amd.yml",
    "docker-compose.nvidia.yml",
    "config/node_roles.json",
    "scripts/update-agent.py",
    "scripts/vertep",
    "scripts/safe-extract.py",
    "scripts/release-layout.py",
    "scripts/apply-deployment.py",
    "scripts/migrate.py",
    "scripts/watchdog.py",
    "scripts/startup-recovery.py",
    "scripts/status.py",
    "scripts/vertep-cli.py",
    "scripts/generate-env.py",
    "scripts/set-env.py",
    "scripts/deployment-plan.py",
    "scripts/runtime-contract.py",
    "installer/detect-gpu.sh",
    "installer/install-comfyui.sh",
    "installer/preflight.sh",
    "installer/role-plan.py",
    "installer/manifest.json",
    "installer/vertep-core.service",
    "installer/vertep-worker.service",
    "installer/vertep-update.service",
    "installer/vertep-update.path",
    "installer/vertep-update.timer",
    "installer/vertep-watchdog.service",
    "installer/vertep-watchdog.timer",
    "installer/vertep-startup-recovery.service",
    "installer/vertep-worker-update.service",
    "installer/vertep-worker-update.path",
    "installer/update-public.pem",
    "installer/root-keys/root-metadata.json",
    "web/index.html",
    "VERSION",
    "CHANGELOG.md",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_manifest(root: Path) -> dict:
    files = {}
    for relative in BUNDLE_FILES:
        path = root / relative
        if not path.exists():
            continue
        files[relative] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    return files


def sign_manifest(manifest: dict, private_key: Path) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    with subprocess.Popen(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, check=True,
    ) as process:
        stdout, _ = process.communicate(canonical)
    return __import__("base64").b64encode(stdout).decode("ascii")


def build_bundle(root: Path, output: Path, version: str, manifest: dict) -> Path:
    bundle_path = output / f"vertep-{version}.tar.gz"
    manifest_path = output / f"manifest-{version}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    files = " ".join(f'"{relative}"' for relative in BUNDLE_FILES if (root / relative).exists())
    bundle_manifest = root / "manifest.json"
    bundle_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    command = f'cd "{root}" && tar -czf "{bundle_path}" {files} manifest.json'
    subprocess.run(command, shell=True, check=True, capture_output=True)
    bundle_manifest.unlink()
    return bundle_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate signed Vertep release bundle")
    parser.add_argument("--root", type=Path, default=Path("."), help="Repository root")
    parser.add_argument("--output", type=Path, default=Path("releases"), help="Output directory")
    parser.add_argument("--private-key", type=Path, help="Release private key for signing")
    parser.add_argument("--version", type=str, help="Release version (e.g. 0.0.0.13)")
    args = parser.parse_args()

    version = args.version or args.root.joinpath("VERSION").read_text(encoding="utf-8").strip()
    match = VERSION_RE.match(version)
    if not match:
        raise SystemExit(f"Invalid version format: {version}")
    version = match.group(1)

    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": version,
        "product": "vertep",
        "schema": 2,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": collect_manifest(args.root),
    }
    if args.private_key and args.private_key.exists():
        manifest["signature"] = sign_manifest(manifest, args.private_key)

    manifest_path = args.output / f"manifest-{version}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"Wrote manifest: {manifest_path}")

    bundle_path = build_bundle(args.root, args.output, version, manifest)
    print(f"Wrote bundle: {bundle_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
