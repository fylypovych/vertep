#!/usr/bin/env python3
"""Crash-safe immutable release preparation and activation.

Release payloads are never overlaid onto the running application.  They are
materialized below ``releases/`` and activation is a single symlink rename.
Mutable appliance data is linked from the installation root into each release.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path


VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?")
MUTABLE = (".env", "config", "logs", "storage", "backups", "models", "tls", "runtime/update")


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _mutable(relative: str) -> bool:
    return any(relative == name or relative.startswith(name + "/") for name in MUTABLE)


def tree_digest(root: Path) -> str:
    """Digest of the release code. The MUTABLE paths are installation data: ``prepare``
    replaces them with links to the shared copies, so they are not part of the release
    (a legacy flat installation carries its storage, renders and their links there)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _mutable(relative):
            continue
        if path.is_symlink():
            raise ValueError(f"release payload contains a symlink: {relative}")
        if path.is_file():
            digest.update(b"F\0" + relative.encode() + b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def prepare(base: Path, payload: Path, version: str) -> Path:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid release version")
    if not payload.is_dir() or not (payload / "VERSION").is_file():
        raise ValueError("release payload is incomplete")
    if (payload / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("release payload version mismatch")
    releases = base / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / version
    if target.exists():
        raise FileExistsError(f"release already exists: {version}")
    # Validate the source before creating any durable staging entry.
    digest = tree_digest(payload)
    temporary = releases / f".{version}.{os.getpid()}.staging"
    try:
        shutil.copytree(payload, temporary, symlinks=True, ignore=lambda directory, names: [
            name for name in names
            if _mutable((Path(directory) / name).relative_to(payload).as_posix())])
        for name in MUTABLE:
            destination = temporary / name
            source = base / name
            source.mkdir(parents=True, exist_ok=True) if "." not in source.name else None
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(os.path.relpath(source, destination.parent))
        _atomic_json(temporary / ".release.json", {"version": version, "sha256": digest})
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _fsync_dir(releases)
    return target


def activate(base: Path, version: str) -> None:
    target = base / "releases" / version
    metadata = json.loads((target / ".release.json").read_text(encoding="utf-8"))
    if metadata.get("version") != version:
        raise ValueError("release metadata mismatch")
    current = base / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    link = base / f".current.{os.getpid()}.tmp"
    link.symlink_to(Path("releases") / version)
    os.replace(link, current)
    _fsync_dir(base)
    _atomic_json(base / ".activation.json", {"current": version, "previous": previous})


def rollback(base: Path) -> None:
    activation = base / ".activation.json"
    if not activation.exists() and not (base / "current").is_symlink():
        # The failure came before any release was activated (a legacy flat installation
        # whose rescue did not complete): the layout is unchanged, nothing to switch back.
        return
    state = json.loads(activation.read_text(encoding="utf-8"))
    previous = state.get("previous")
    if not isinstance(previous, str) or not (base / previous).is_dir():
        raise RuntimeError("no immutable previous release is available")
    link = base / f".current.{os.getpid()}.tmp"
    link.symlink_to(previous)
    os.replace(link, base / "current")
    _fsync_dir(base)
    _atomic_json(base / ".activation.json", {"current": Path(previous).name, "previous": None})


def discard(base: Path, version: str) -> bool:
    """Remove a release left behind by a failed attempt so the same version can be retried.

    A failed apply is rolled back by re-activating the previous release, but the prepared
    tree of the failed version stays in ``releases/``; ``prepare`` then refuses any retry
    of that version. Only a release that is neither active nor the rollback target is
    removed; anything still referenced is refused.
    """
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid release version")
    target = base / "releases" / version
    if not target.exists():
        return False
    referenced = set()
    if (base / "current").is_symlink():
        referenced.add(Path(os.readlink(base / "current")).name)
    try:
        state = json.loads((base / ".activation.json").read_text(encoding="utf-8"))
        referenced.update(Path(str(state.get(key) or "")).name for key in ("current", "previous"))
    except (OSError, ValueError):
        pass
    if version in referenced:
        raise RuntimeError(f"release {version} is active or the rollback target")
    shutil.rmtree(target)
    _fsync_dir(base / "releases")
    return True


def prune(base: Path, keep: int = 3) -> list[str]:
    """Remove old inactive releases while preserving active and rollback targets."""
    if keep < 2:
        raise ValueError("at least two releases must be retained")
    releases = base / "releases"
    state_path = base / ".activation.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    protected = {str(state.get("current", "")), Path(str(state.get("previous", ""))).name}
    if (base / "current").is_symlink():
        protected.add(Path(os.readlink(base / "current")).name)
    candidates = [path for path in releases.iterdir() if path.is_dir() and not path.name.startswith(".")]
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    protected.update(path.name for path in candidates[:keep])
    removed = []
    for path in candidates:
        if path.name not in protected:
            shutil.rmtree(path)
            removed.append(path.name)
    if removed:
        _fsync_dir(releases)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "activate", "rollback", "prune", "discard"))
    parser.add_argument("base", type=Path)
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    if args.command == "prepare":
        payload, version = args.argument.split("::", 1)
        print(prepare(args.base.resolve(), Path(payload).resolve(), version))
    elif args.command == "activate":
        activate(args.base.resolve(), args.argument)
    elif args.command == "rollback":
        rollback(args.base.resolve())
    elif args.command == "discard":
        print(json.dumps({"discarded": discard(args.base.resolve(), args.argument)}))
    else:
        print(json.dumps({"removed": prune(args.base.resolve(), int(args.argument or "3"))}))


if __name__ == "__main__":
    main()
