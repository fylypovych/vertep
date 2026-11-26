"""Build, publish and sign the «characters» Application Module package (#499).

    python modules/characters/build.py --version 1.0.0 --registry localhost:5000 \
        --key signer.pem --signer acme-k1 --publisher vertep --repository github.com/fylypovych/vertep \
        --output characters-1.0.0.json

The image is pushed to ``--registry`` and the manifest pins it by the pushed
sha256 digest; the Ed25519 signature covers the manifest, the migrations and
the UI file, as required by docs/architecture/contracts/module_contract.md.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATIONS = {
    "1.0.0": ["m001_initial"],
    "0.0.4.18": ["m001_initial", "m002_knowledge_memory"],
}


def image(version: str, registry: str) -> str:
    tag = f"{registry}/vertep/characters:{version}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(HERE)], check=True, capture_output=True, timeout=900)
    subprocess.run(["docker", "push", "-q", tag], check=True, capture_output=True, timeout=900)
    digests = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{json .RepoDigests}}"],
        check=True, capture_output=True, text=True
    ).stdout
    prefix = f"{registry}/vertep/characters@"
    return next(item for item in json.loads(digests) if item.startswith(prefix))


def package(version: str, pinned_image: str, private_key, signer: str, publisher: str, repository: str) -> dict:
    ui = (HERE / "ui/index.html").read_bytes()
    files = {"index.html": base64.b64encode(ui).decode()}
    for migration in MIGRATIONS[version]:
        files[f"migrations/{migration}.sql"] = base64.b64encode((HERE / "migrations" / f"{migration}.sql").read_bytes()).decode()
    manifest = {
        "schema_version": "2.0",
        "name": "characters",
        "module_id": "characters",
        "version": version,
        "kind": "application",
        "core_compatibility": {"min": "0.0.3.91", "max": "0.0.9.99"},
        "dependencies": [],
        "capabilities": ["characters_api"],
        "resource_requirements": {"min_vram_mb": 0, "min_cpu_cores": 1, "min_ram_mb": 512},
        "placement": {"target": "any", "scaling": "single_owner", "requires_gpu": False},
        "provides": [{"type": "api", "name": "characters", "version": "1.0"}],
        "requires": [],
        "api": {"namespace": "characters", "version": "1.0", "routes": [
            "/characters",
            "/characters/{id}",
            "/characters/{id}/references",
            "/characters/{id}/references/{filename}",
            "/characters/{id}/collaborations",
            "/characters/{id}/collaborations/{brand_id}",
            "/characters/{id}/activate",
            "/characters/{id}/deactivate",
            "/characters/{id}/can-generate",
            "/characters/{id}/knowledge/sources",
            "/characters/{id}/knowledge/sources/{source_id}",
            "/characters/{id}/knowledge/sources/{source_id}/chunks",
            "/characters/{id}/knowledge/sources/{source_id}/rights",
            "/characters/{id}/knowledge/items",
            "/characters/{id}/knowledge/items/{item_id}",
            "/characters/{id}/knowledge/items/{item_id}/approve",
            "/characters/{id}/knowledge/items/{item_id}/reject",
            "/characters/{id}/knowledge/revocations",
            "/characters/{id}/knowledge/policies",
            "/characters/{id}/knowledge/verify-rights",
            "/characters/{id}/knowledge/incidents",
            "/characters/{id}/knowledge/snapshots/{snapshot_id}",
            "/characters/{id}/context",
            "/characters/{id}/timelines",
            "/characters/{id}/timelines/transfer",
            "/characters/{id}/canon/publish",
            "/characters/{id}/canon/versions",
            "/characters/{id}/canon/migrate",
        ]},
        "ui": {"entry": "index.html", "navigation": [{"id": "characters", "label": "Персонажі"}],
               "files": {"index.html": hashlib.sha256(ui).hexdigest()}},
        "permissions": ["characters.read", "characters.write", "characters.collaborate"],
        "secret_refs": [],
        "migrations": [{"id": migration, "version": version} for migration in MIGRATIONS[version]],
        "services": [{"name": "api"}],
        "health": {"self_test": "health", "timeout_seconds": 30},
        "publisher": publisher,
        "provenance": {"repository": repository},
        "container": {"image": pinned_image, "port": 8080},
    }
    content = {"manifest": manifest, "files": files}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {**content, "signer": signer, "signature": base64.b64encode(private_key.sign(raw)).decode()}


def main(argv=None) -> int:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    parser = argparse.ArgumentParser()
    for name in ("version", "registry", "key", "signer", "publisher", "repository", "output"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    if args.version not in MIGRATIONS:
        parser.error(f"unknown version; choose {sorted(MIGRATIONS)}")
    key = load_pem_private_key(Path(args.key).read_bytes(), None)
    result = package(args.version, image(args.version, args.registry), key, args.signer, args.publisher, args.repository)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())