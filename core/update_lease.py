"""Crash-safe local update lease used to fence concurrent update agents."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path


class UpdateLease:
    def __init__(self, state_dir: Path, operation_id: str):
        self.path = state_dir / "update.lock"
        self.operation_id = operation_id
        self._file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._file.close()
            self._file = None
            raise RuntimeError("Another update operation holds the update lease") from error
        self._file.seek(0)
        self._file.truncate()
        json.dump({"operation_id": self.operation_id, "pid": os.getpid(),
                   "acquired_at": datetime.now(timezone.utc).isoformat()}, self._file)
        self._file.flush()
        os.fsync(self._file.fileno())
        return self

    def __exit__(self, *_):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
