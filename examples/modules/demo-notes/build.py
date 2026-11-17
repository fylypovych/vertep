"""Build, publish and sign the «demo-notes» Application Module package (#495).

    python examples/modules/demo-notes/build.py --version 1.0.0 --registry localhost:5000 \
        --key signer.pem --signer acme-k1 --publisher acme --repository github.com/acme/demo-notes \
        --output demo-notes-1.0.0.json

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
MIGRATIONS = {"1.0.0": ["m001_notes"], "1.1.0": ["m001_notes", "m002_reviewed"]}


def image(version: str, registry: str) -> str:
    tag = f"{registry}/vertep/demo-notes:{version}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(HERE)], check=True, capture_output=True, timeout=900)
    subprocess.run(["docker", "push", "-q", tag], check=True, capture_output=True, timeout=900)
    digests = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{json .RepoDigests}}"],
                             check=True, capture_output=True, text=True).stdout
    prefix = f"{registry}/vertep/demo-notes@"
    return next(item for item in json.loads(digests) if item.startswith(prefix))


def package(version: str, pinned_image: str, private_key, signer: str, publisher: str, repository: str) -> dict:
    ui = (HERE / "ui/index.html").read_bytes()
    files = {"index.html": base64.b64encode(ui).decode()}
    for migration in MIGRATIONS[version]:
        files[f"migrations/{migration}.sql"] = base64.b64encode((HERE / "migrations" / f"{migration}.sql").read_bytes()).decode()
    manifest = {
        "schema_version": "2.0", "name": "demo-notes", "version": version, "kind": "application",
        "description": "Демонстраційний модуль контракту модулів Vertep (без бізнес-логіки).",
        "publisher": publisher, "provenance": {"repository": repository},
        "container": {"image": pinned_image, "port": 8080},
        "capabilities": [], "dependencies": [],
        "resource_requirements": {"min_vram_mb": 0, "min_cpu_cores": 1, "min_ram_mb": 128},
        "requires": [{"type": "capability", "name": "text_generation", "min_version": "1.0"}],
        "api": {"namespace": "demo-notes", "version": "1.0", "routes": ["/notes"]},
        "ui": {"entry": "index.html", "navigation": [{"id": "demo-notes", "label": "Демо-нотатки"}],
               "files": {"index.html": hashlib.sha256(ui).hexdigest()}},
        "permissions": ["demo-notes.data"],
        "migrations": [{"id": migration, "version": version} for migration in MIGRATIONS[version]],
        "services": [{"name": "summarizer"}],
        "health": {"self_test": "health", "timeout_seconds": 90},
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
