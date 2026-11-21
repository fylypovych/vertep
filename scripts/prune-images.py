#!/usr/bin/env python3
"""Remove old local Vertep images after a successful update (#517).

Old images piled up on the host after every update (154 images on one stand, `<none>`
entries that `dangling=true` did not even report under the containerd snapshotter).

  record --root R --version V   after a successful install/update: remember the version and
                                every image reference in .env (config/installed-versions.json)
  prune  --root R [--keep 3] [--dry-run]
                                remove Vertep images outside the last `keep` successful
                                versions (current included)

An image is removed only when ALL hold:
  * it belongs to Vertep: every repository it carries is a Vertep repository, or an untagged
    (`<none>`) image carries the label org.opencontainers.image.source of the Vertep repo;
  * no kept version references it (by digest or tag);
  * no container, running or stopped, uses it.
Other projects' images, containers and volumes are never touched. `dangling=true` is not used:
every local image is inspected. Nothing is forced: `docker image rm` refuses anything in use.
Run only after success: a failed update keeps every version available for rollback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RECORD = Path("config") / "installed-versions.json"
SOURCE_LABEL = "org.opencontainers.image.source"
VERTEP_SOURCE = "https://github.com/fylypovych/vertep"
DEFAULT_PREFIXES = ("ghcr.io/fylypovych/vertep-",)
IMAGE_KEY = re.compile(r"^VERTEP_[A-Z0-9_]*IMAGE$")


def _env(root: Path) -> dict[str, str]:
    values = {}
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _load(root: Path) -> list[dict]:
    try:
        data = json.loads((root / RECORD).read_text(encoding="utf-8"))
        return [item for item in data.get("versions", []) if isinstance(item, dict) and item.get("version")]
    except (OSError, ValueError, AttributeError):
        return []


def record(root: Path, version: str, now: float | None = None) -> dict:
    images = sorted({value for key, value in _env(root).items() if IMAGE_KEY.match(key) and value})
    versions = [item for item in _load(root) if item["version"] != version]
    versions.append({"version": version, "images": images, "recorded_at": now or time.time()})
    path = root / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"versions": versions[-10:]}, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return {"version": version, "images": len(images)}


def _repository(reference: str) -> str:
    """repo from repo:tag, repo@digest or repo:tag@digest (a registry port is kept)."""
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(last)] + last.split(":", 1)[0]


def _digest(reference: str) -> str | None:
    return reference.split("@", 1)[1] if "@" in reference else None


def _docker(runner, *args: str) -> str:
    return runner(["docker", *args], capture_output=True, text=True, check=True).stdout


def prune(root: Path, keep: int = 3, dry_run: bool = False, runner=subprocess.run) -> dict:
    if keep < 1:
        raise ValueError("at least the current version is kept")
    versions = _load(root)
    kept_versions = versions[-keep:]
    kept_refs = {ref for item in kept_versions for ref in item.get("images", [])}
    kept_refs.update(value for key, value in _env(root).items() if IMAGE_KEY.match(key) and value)  # current, always
    if len(versions) < keep:
        # Versions installed before this record existed: keep what the host still references
        # for them (immutable releases and bootstrap snapshots) instead of guessing.
        for manifest in (root / "releases").glob("*/manifest.json"):
            try:
                images = json.loads(manifest.read_text(encoding="utf-8")).get("images") or {}
                kept_refs.update(f"{item['reference']}@{item['digest']}" for item in images.values()
                                 if isinstance(item, dict) and item.get("reference") and item.get("digest"))
            except (OSError, ValueError):
                continue
        for env in (root / "backups").glob("bootstrap-*/.env"):
            kept_refs.update(value for key, value in _env(env.parent).items() if IMAGE_KEY.match(key) and value)
    kept_digests = {(_repository(ref), _digest(ref)) for ref in kept_refs if _digest(ref)}
    kept_tags = {ref.split("@", 1)[0] for ref in kept_refs if ":" in ref.split("@", 1)[0].rsplit("/", 1)[-1]}
    prefixes = set(DEFAULT_PREFIXES)
    repository = _env(root).get("VERTEP_IMAGE_REPOSITORY")
    if repository:
        prefixes.add(repository.rstrip("/") + "/")
    vertep_repos = {_repository(ref) for item in versions for ref in item.get("images", [])} | {
        _repository(ref) for ref in kept_refs}

    def vertep_repo(repo: str) -> bool:
        return repo in vertep_repos or any(repo.startswith(prefix) for prefix in prefixes)

    in_use = set()
    containers = _docker(runner, "ps", "-aq", "--no-trunc").split()
    if containers:
        in_use = set(_docker(runner, "inspect", "--format", "{{.Image}}", *containers).split())
    image_ids = sorted(set(_docker(runner, "image", "ls", "-aq", "--no-trunc").split()))
    report = {"removed": [], "kept": 0, "foreign": 0, "in_use": 0, "kept_versions": [item["version"] for item in kept_versions],
              "freed_bytes": 0, "dry_run": dry_run}
    if not image_ids:
        return report
    for image in json.loads(_docker(runner, "image", "inspect", *image_ids)):
        repos = {_repository(tag) for tag in image.get("RepoTags") or [] if tag != "<none>:<none>"}
        repos |= {_repository(digest) for digest in image.get("RepoDigests") or []}
        labels = ((image.get("Config") or {}).get("Labels") or {})
        owned = (all(vertep_repo(repo) for repo in repos) if repos
                 else labels.get(SOURCE_LABEL) == VERTEP_SOURCE)
        if not owned:
            report["foreign"] += 1
            continue
        if image["Id"] in in_use:
            report["in_use"] += 1
            continue
        referenced = any((_repository(digest), _digest(digest)) in kept_digests for digest in image.get("RepoDigests") or [])
        referenced |= any(tag in kept_tags for tag in image.get("RepoTags") or [])
        if referenced:
            report["kept"] += 1
            continue
        name = (image.get("RepoTags") or image.get("RepoDigests") or ["<none>"])[0]
        if not dry_run:
            result = runner(["docker", "image", "rm", image["Id"]], capture_output=True, text=True, check=False)
            if result.returncode != 0:
                report.setdefault("refused", []).append({"image": name, "error": result.stderr.strip()[:200]})
                continue
        report["removed"].append(name)
        report["freed_bytes"] += int(image.get("Size") or 0)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--root", type=Path, required=True)
    rec.add_argument("--version", required=True)
    pr = sub.add_parser("prune")
    pr.add_argument("--root", type=Path, required=True)
    pr.add_argument("--keep", type=int, default=int(os.getenv("VERTEP_IMAGE_KEEP_VERSIONS", "3")))
    pr.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "record":
        print(json.dumps(record(args.root.resolve(), args.version)))
    else:
        print(json.dumps(prune(args.root.resolve(), args.keep, args.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
