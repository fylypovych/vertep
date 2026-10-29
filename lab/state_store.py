"""Durable, crash-safe state primitives for the laboratory.

The laboratory keeps its own configuration, queue, journals and artifacts on
disk; it deliberately does not create tables or states inside production
Vertep.  Writes are atomic (temp file + replace) so a restart or a killed
container never leaves a half-written record, and appends are single ``write``
calls on an ``O_APPEND`` handle so concurrent readers never see torn lines.
"""

import json
import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

_append_locks: dict[str, threading.Lock] = {}
_append_locks_guard = threading.Lock()
_transaction_locks = {}
_transaction_depth = threading.local()


def state_transaction(lock_name):
    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            with exclusive(self.state_dir / lock_name):
                return method(self, *args, **kwargs)
        return call
    return decorate


@contextmanager
def exclusive(path):
    """Reentrant process/thread exclusion; lock files are retained, never unlinked."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _append_locks_guard:
        lock = _transaction_locks.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_transaction_depth, "values", {})
        if depths.get(key, 0):
            yield
            return
        _transaction_depth.values = depths
        with path.open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0"); stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_json(path: Path, default: Any = None) -> Any:
    """Return the JSON document at ``path`` or ``default`` when unusable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    """Atomically publish ``value`` as JSON at ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    if mode is not None:
        os.chmod(temporary, mode)
    temporary.replace(path)


def append_jsonl(path: Path, record: Any) -> None:
    """Append one JSON line; used for append-only journals and audit trails."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    key = str(path)
    with _append_locks_guard:
        lock = _append_locks.setdefault(key, threading.Lock())
    with lock:
        with path.open("a", encoding="utf-8") as output:
            output.write(line)
            output.flush()
            os.fsync(output.fileno())


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL journal, skipping unparsable trailing lines."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
