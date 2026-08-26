#!/usr/bin/env python3
"""Validate and extract a Vertep release archive without following archive links."""

import argparse
import os
import tarfile
from pathlib import Path, PurePosixPath


def safe_extract(archive: Path, destination: Path, maximum_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            normalized = path.as_posix().removeprefix("./")
            if (not normalized or path.is_absolute() or ".." in path.parts or normalized in seen
                    or member.issym() or member.islnk() or member.isdev() or member.isfifo()
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f"Unsafe archive member: {member.name}")
            seen.add(normalized)
            total += member.size
            if total > maximum_bytes:
                raise ValueError("Uncompressed update exceeds configured size limit")
        for member in members:
            relative = PurePosixPath(member.name).as_posix().removeprefix("./")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"Archive member has no data: {member.name}")
            temporary = target.with_name(f".{target.name}.tmp")
            with temporary.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            temporary.chmod(0o755 if member.mode & 0o111 else 0o644)
            temporary.replace(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--max-bytes", type=int,
                        default=int(os.getenv("UPDATE_MAX_UNCOMPRESSED_BYTES", str(8 * 1024**3))))
    arguments = parser.parse_args()
    safe_extract(arguments.archive, arguments.destination, arguments.max_bytes)
