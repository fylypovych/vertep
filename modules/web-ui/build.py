#!/usr/bin/env python3
import subprocess
import sys
import os
import hashlib
import json
from pathlib import Path

def run(cmd, cwd=None):
    print(f"> {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}")
    return result

def main():
    module_dir = Path(__file__).parent
    version = "1.0.0"
    image_name = f"vertep/web-ui:{version}"
    package_name = f"vertep-web-ui-{version}.tar.gz"

    print(f"Building {image_name}...")
    run(f"docker build -t {image_name} .", cwd=module_dir)

    print("Saving image...")
    run(f"docker save {image_name} | gzip > {package_name}", cwd=module_dir)

    print("Calculating SHA256...")
    with open(module_dir / package_name, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()

    print(f"SHA256: {sha256}")

    manifest = {
        "module": "web-ui",
        "version": version,
        "image": image_name,
        "package": package_name,
        "sha256": sha256,
        "schema_version": "2.0",
        "kind": "application",
    }

    with open(module_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to manifest.json")
    print(f"Package: {module_dir / package_name}")

if __name__ == "__main__":
    main()