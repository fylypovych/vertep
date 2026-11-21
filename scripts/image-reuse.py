#!/usr/bin/env python3
"""Reuse a released image whose inputs did not change (owner decision 21.11.2026).

A release used to rebuild all images even when only one changed. Now every built image
carries the label ``org.vertep.inputs`` — a SHA-256 over its Dockerfile, every file the
Dockerfile copies from the build context, its platforms and its constant build args.
The next release computes the same value; when the image published as ``:latest`` has
it and is not older than ``--max-age-days`` (so base images and packages still get
refreshed), the release only adds its version tag to that digest instead of building.

Images that bake the Vertep version in (``ARG VERTEP_VERSION`` / ``VERTEP_RELEASE_SHA``)
are always rebuilt: a reused one would report the previous version.

  fingerprint --file F --context C --platforms P   -> prints the inputs hash
  decide --fingerprint X --inspect J [--max-age-days N] [--file F]
                                                   -> prints {"reuse": bool, "reason": ...}
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

LABEL = "org.vertep.inputs"
BUILT_AT = "org.vertep.built-at"
VERSION_ARGS = {"VERTEP_VERSION", "VERTEP_RELEASE_SHA"}
CONSTANT_ARGS = ("VERTEP_ENVIRONMENT=production",)


def _instructions(dockerfile: Path) -> list[str]:
    lines, current = [], ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        lines.append(current + line)
        current = ""
    if current:
        lines.append(current)
    return lines


def bakes_version(dockerfile: Path) -> bool:
    return any(re.match(r"ARG\s+(%s)\b" % "|".join(VERSION_ARGS), line, re.I) for line in _instructions(dockerfile))


def copy_sources(dockerfile: Path) -> list[str]:
    """Build-context sources of COPY/ADD (``--from`` copies come from other stages)."""
    sources = []
    for line in _instructions(dockerfile):
        match = re.match(r"(COPY|ADD)\s+(.*)$", line, re.I)
        if not match:
            continue
        rest = match.group(2).strip()
        if rest.startswith("["):
            parts = json.loads(rest)
        else:
            parts = shlex.split(rest)
        flags = [part for part in parts if part.startswith("--")]
        if any(flag.startswith("--from") for flag in flags):
            continue
        args = [part for part in parts if not part.startswith("--")]
        sources.extend(args[:-1])
    return sources


def _files(context: Path, source: str) -> list[Path]:
    if re.match(r"^[a-z]+://", source):
        raise ValueError(f"Remote ADD source makes the inputs unknowable: {source}")
    pattern = source.lstrip("./") or "."
    if any(char in pattern for char in "*?["):
        matches = [path for path in context.rglob("*") if fnmatch.fnmatch(str(path.relative_to(context)), pattern)]
    else:
        matches = [context / pattern]
    result = []
    for match in matches:
        if match.is_dir():
            result.extend(path for path in match.rglob("*") if path.is_file() and "__pycache__" not in path.parts
                          and "node_modules" not in path.parts)
        elif match.is_file():
            result.append(match)
        else:
            raise ValueError(f"COPY source does not exist: {source}")
    return result


def fingerprint(dockerfile: Path, context: Path, platforms: str) -> str:
    context = context.resolve()
    files = {dockerfile.resolve()}
    for source in copy_sources(dockerfile):
        files.update(path.resolve() for path in _files(context, source))
    digest = hashlib.sha256()
    digest.update(f"platforms={platforms}\n".encode())
    for arg in CONSTANT_ARGS:
        digest.update(f"arg={arg}\n".encode())
    for path in sorted(files):
        try:
            name = str(path.relative_to(context))
        except ValueError:
            name = str(path)
        digest.update(f"{name}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()


def _labels(inspect: object) -> list[dict]:
    """Labels of every platform config in `docker buildx imagetools inspect --format '{{json .Image}}'`."""
    configs = list(inspect.values()) if isinstance(inspect, dict) and "config" not in inspect else [inspect]
    return [((item or {}).get("config") or {}).get("Labels") or {} for item in configs if isinstance(item, dict)]


def decide(value: str, inspect: object, max_age_days: float, now: datetime | None = None,
           dockerfile: Path | None = None) -> dict:
    if dockerfile is not None and bakes_version(dockerfile):
        return {"reuse": False, "reason": "image bakes the Vertep version in"}
    labels = _labels(inspect)
    if not labels:
        return {"reuse": False, "reason": "no previous image"}
    if any(item.get(LABEL) != value for item in labels):
        return {"reuse": False, "reason": "inputs changed"}
    try:
        built = min(datetime.fromisoformat(item[BUILT_AT].replace("Z", "+00:00")) for item in labels)
    except (KeyError, ValueError):
        return {"reuse": False, "reason": "previous image has no build time"}
    age = ((now or datetime.now(timezone.utc)) - built).total_seconds() / 86400
    if age > max_age_days:
        return {"reuse": False, "reason": f"previous image is {age:.1f} days old (base images refresh)"}
    return {"reuse": True, "reason": f"inputs unchanged, built {age:.1f} days ago"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("fingerprint")
    fp.add_argument("--file", type=Path, required=True)
    fp.add_argument("--context", type=Path, required=True)
    fp.add_argument("--platforms", required=True)
    de = sub.add_parser("decide")
    de.add_argument("--fingerprint", required=True)
    de.add_argument("--inspect", type=Path, required=True, help="imagetools inspect JSON; missing/empty = no image")
    de.add_argument("--max-age-days", type=float, default=7)
    de.add_argument("--file", type=Path)
    args = parser.parse_args()
    if args.command == "fingerprint":
        print(fingerprint(args.file, args.context, args.platforms))
        return
    try:
        inspect = json.loads(args.inspect.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        inspect = None
    print(json.dumps(decide(args.fingerprint, inspect, args.max_age_days, dockerfile=args.file)))


if __name__ == "__main__":
    sys.exit(main())
