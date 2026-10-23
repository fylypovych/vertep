#!/usr/bin/env python3
"""Build and execute the standard runtime image without a source checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    expected_sha = os.getenv("GITHUB_SHA", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise SystemExit("GITHUB_SHA must be the full 40-character CI commit SHA")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit("VERSION is empty")
    image = f"vertep/runtime-identity-check:{expected_sha}"
    subprocess.run(
        [
            "docker", "build", "--file", "Dockerfile",
            "--build-arg", f"VERTEP_VERSION={version}",
            "--build-arg", f"VERTEP_RELEASE_SHA={expected_sha}",
            "--build-arg", "VERTEP_ENVIRONMENT=ci",
            "--tag", image, ".",
        ],
        cwd=ROOT,
        check=True,
    )
    probe = r"""
import json
from pathlib import Path
from core.release_identity import (
    resolve_release_environment, resolve_release_sha, resolve_release_version,
)
identity = {
    "version": resolve_release_version(),
    "commit_sha": resolve_release_sha(),
    "environment": resolve_release_environment(),
    "git_directory_present": Path("/app/.git").exists(),
}
print(json.dumps(identity, sort_keys=True))
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        actual = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise SystemExit(f"runtime identity probe returned invalid output: {result.stdout!r}") from exc
    expected = {
        "version": version,
        "commit_sha": expected_sha,
        "environment": "ci",
        "git_directory_present": False,
    }
    if actual != expected:
        raise SystemExit(f"runtime identity mismatch: expected={expected!r}, actual={actual!r}")

    evidence = {
        "criterion": "i.0.0.2.84 / #187.1-#187.2",
        "expected": expected,
        "actual": actual,
        "image": image,
        "git_directory_present": actual["git_directory_present"],
        "result": "PASS",
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
