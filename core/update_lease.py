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
        self._database = None
        self.fence_epoch = None

    def _acquire_distributed(self) -> None:
        dsn = os.getenv("UPDATE_DATABASE_URL", os.getenv("DATABASE_URL", ""))
        required = os.getenv("UPDATE_REQUIRE_DISTRIBUTED_FENCE", "").lower() == "true"
        if not dsn:
            if required:
                raise RuntimeError("Distributed update fencing requires UPDATE_DATABASE_URL")
            return
        try:
            import psycopg
            self._database = psycopg.connect(dsn, autocommit=True)
            acquired = self._database.execute(
                "SELECT pg_try_advisory_lock(%s)", (37031809926992,)).fetchone()[0]
            if not acquired:
                raise RuntimeError("Another CORE replica holds the distributed update fence")
            self._database.execute("""CREATE TABLE IF NOT EXISTS update_fences(
                name TEXT PRIMARY KEY, epoch BIGINT NOT NULL, operation_id TEXT NOT NULL,
                owner TEXT NOT NULL, acquired_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            row = self._database.execute("""INSERT INTO update_fences(name,epoch,operation_id,owner,acquired_at)
                VALUES('global',1,%s,%s,now()) ON CONFLICT(name) DO UPDATE SET
                epoch=update_fences.epoch+1,operation_id=excluded.operation_id,
                owner=excluded.owner,acquired_at=excluded.acquired_at RETURNING epoch""",
                (self.operation_id, f"{os.uname().nodename if hasattr(os, 'uname') else 'windows'}:{os.getpid()}"),
            ).fetchone()
            self.fence_epoch = int(row[0])
        except Exception:
            if self._database is not None:
                self._database.close()
                self._database = None
            raise

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+", encoding="utf-8")
        try:
            _lock(self._file)
        except BlockingIOError as error:
            self._file.close()
            self._file = None
            raise RuntimeError("Another update operation holds the update lease") from error
        try:
            self._acquire_distributed()
        except Exception:
            _unlock(self._file)
            self._file.close()
            self._file = None
            raise
        metadata = {"operation_id": self.operation_id, "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "fence_epoch": self.fence_epoch}
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
        if self._database is not None:
            try:
                self._database.execute("SELECT pg_advisory_unlock(%s)", (37031809926992,))
            finally:
                self._database.close()
                self._database = None
        if self._file is not None:
            _unlock(self._file)
            self._file.close()
            self._file = None
