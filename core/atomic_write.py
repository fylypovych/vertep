"""Single shared implementation of write-to-temp-then-replace file writes.

Every caller in this codebase wrote its own copy of this pattern with slightly
different flourishes (json vs bytes, chmod vs not, pid-suffixed tmp name vs
not). This module keeps exactly one implementation and lets callers opt into
the pieces they need.
"""

import json
import os
import time
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write bytes to `path` without ever exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if mode is not None:
        os.chmod(temporary, mode)
    _replace(temporary, path)


def _replace(temporary: Path, path: Path) -> None:
    """Swap the file in; on Windows retry briefly while another process holds the target.

    POSIX replaces an open file atomically. Windows refuses with PermissionError while a
    reader or an antivirus scan has the target open, so a concurrent read made a write
    fail (#439). The retry is bounded; a lasting refusal is raised as before.
    """
    for attempt in range(20 if os.name == "nt" else 1):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None,
                      encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path: Path, value: Any, *, mode: int | None = None,
                      indent: int | None = 2, ensure_ascii: bool = False) -> None:
    atomic_write_text(path, json.dumps(value, indent=indent, ensure_ascii=ensure_ascii), mode=mode)
