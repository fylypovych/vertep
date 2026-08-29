"""Crash-safe local update lease used to fence concurrent update agents."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _lock(file) -> None:
    if os.name == "nt":
        file.seek(0)
        file.write("\0")
        file.flush()
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError from error
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(file) -> None:
    if os.name == "nt":
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class UpdateLease:
    def __init__(self, state_dir: Path, operation_id: str):
        self.path = state_dir / "update.lock"
        self.lock_path = (state_dir / "update.lock.guard") if os.name == "nt" else self.path
        self.operation_id = operation_id
        self._file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+", encoding="utf-8")
        try:
            _lock(self._file)
        except BlockingIOError as error:
            self._file.close()
            self._file = None
            raise RuntimeError("Another update operation holds the update lease") from error
        metadata = {"operation_id": self.operation_id, "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat()}
        if os.name == "nt":
            self.path.write_text(json.dumps(metadata), encoding="utf-8")
        else:
            self._file.seek(0)
            self._file.truncate()
            json.dump(metadata, self._file)
            self._file.flush()
            os.fsync(self._file.fileno())
        return self

    def __exit__(self, *_):
        if self._file is not None:
            _unlock(self._file)
            self._file.close()
            self._file = None
