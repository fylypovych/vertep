"""Snapshot класу для backup/restore стану CORE з підтримкою незавершених jobs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Snapshot:
    """Знімок стану systems: jobs, tasks, artifacts, settings, checksum."""

    snapshot_id: str
    created_at: str
    version: str
    jobs: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""
    incremental: bool = False
    parent_snapshot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "version": self.version,
            "jobs": self.jobs,
            "tasks": self.tasks,
            "artifacts": self.artifacts,
            "settings": self.settings,
            "checksum": self.checksum,
            "incremental": self.incremental,
            "parent_snapshot_id": self.parent_snapshot_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            created_at=str(data["created_at"]),
            version=str(data.get("version", "0.0.0.0")),
            jobs=data.get("jobs", []),
            tasks=data.get("tasks", []),
            artifacts=data.get("artifacts", []),
            settings=data.get("settings", {}),
            checksum=str(data.get("checksum", "")),
            incremental=bool(data.get("incremental", False)),
            parent_snapshot_id=data.get("parent_snapshot_id"),
        )

    def compute_checksum(self) -> str:
        """Обчислити SHA256 контрольну суму вмісту знімку."""
        payload = {
            "jobs": self.jobs,
            "tasks": self.tasks,
            "artifacts": self.artifacts,
            "settings": self.settings,
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify(self) -> bool:
        """Перевірити цілісність знімку за контрольною сумою."""
        expected = self.checksum or self.compute_checksum()
        return self.compute_checksum() == expected

    @classmethod
    def create(cls, jobs: list[dict[str, Any]], tasks: list[dict[str, Any]],
               artifacts: list[dict[str, Any]], settings: dict[str, Any],
               version: str, incremental: bool = False,
               parent_snapshot_id: str | None = None) -> "Snapshot":
        """Створити новий знімок стану."""
        snapshot_id = f"snap-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        snapshot = cls(
            snapshot_id=snapshot_id,
            created_at=now,
            version=version,
            jobs=jobs,
            tasks=tasks,
            artifacts=artifacts,
            settings=settings,
            incremental=incremental,
            parent_snapshot_id=parent_snapshot_id,
        )
        snapshot.checksum = snapshot.compute_checksum()
        return snapshot
