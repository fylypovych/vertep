"""Сховище знімків: локальна файлова система (S3 — опціонально)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .snapshot import Snapshot


class SnapshotStorage:
    """Збереження/завантаження/видалення/список знімків."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("BACKUP_ROOT", "backups"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, snapshot_id: str) -> Path:
        return self.root / f"{snapshot_id}.json"

    def save_snapshot(self, snapshot: Snapshot) -> str:
        """Зберегти знімок та повернути його ID."""
        path = self._path(snapshot.snapshot_id)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return snapshot.snapshot_id

    def load_snapshot(self, snapshot_id: str) -> Snapshot:
        """Завантажити знімок за ID."""
        path = self._path(snapshot_id)
        if not path.is_file():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot.from_dict(data)

    def list_snapshots(self) -> list[dict[str, Any]]:
        """Повернути список всіх знімків з метаданими."""
        result: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("snap-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append({
                    "snapshot_id": data.get("snapshot_id"),
                    "created_at": data.get("created_at"),
                    "version": data.get("version"),
                    "incremental": data.get("incremental", False),
                    "parent_snapshot_id": data.get("parent_snapshot_id"),
                    "jobs_count": len(data.get("jobs", [])),
                    "tasks_count": len(data.get("tasks", [])),
                    "checksum": data.get("checksum"),
                })
            except (OSError, ValueError):
                continue
        return result

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Видалити знімок за ID. Повертає True якщо видалений."""
        path = self._path(snapshot_id)
        if path.is_file():
            path.unlink()
            return True
        return False
