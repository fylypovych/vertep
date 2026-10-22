"""Migration utilities for Architecture 2.0 version transitions.

Provides automatic migration between different versions of the system,
handling configuration updates, data migration, and backward compatibility.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class MigrationManager:
    """Manages automated migrations between system versions."""

    def __init__(self, working_dir: Path) -> None:
        self.working_dir = working_dir
        self._migration_log: list[dict] = []

    def migrate_to_version(self, target_version: str) -> dict[str, Any]:
        """Migrate system to target version.

        Args:
            target_version: Target version in A.B.C.D format

        Returns:
            Migration result with status, changed files, and warnings
        """
        result: dict[str, Any] = {
            "status": "pending",
            "target_version": target_version,
            "changed_files": [],
            "warnings": [],
            "errors": [],
        }

        # Find available migrations
        migrations_dir = self.working_dir / "migrations"
        if not migrations_dir.exists():
            result["errors"].append("Migrations directory not found")
            result["status"] = "error"
            return result

        # Get current version
        current_version = self._get_current_version()
        result["current_version"] = current_version

        if current_version == target_version:
            result["status"] = "already_at_target"
            return result

        # Run migrations in sequence
        for migration_file in sorted(migrations_dir.glob("*.py")):
            try:
                migration_module = __import__(
                    f"core.migration.{migration_file.stem}",
                    fromlist=["MigrationStep"],
                )
                step = migration_module.MigrationStep(
                    current_version, target_version, self.working_dir
                )
                if step.is_applicable():
                    changes = step.apply()
                    result["changed_files"].extend(changes)
                    self._migration_log.append(
                        {"step": migration_file.name, "version": step.new_version}
                    )
            except Exception as e:
                result["errors"].append(f"Migration {migration_file.name}: {e}")
                result["status"] = "error"
                break

        if not result["errors"] and result["changed_files"]:
            result["status"] = "completed"
        elif not result["changed_files"] and not result["errors"]:
            result["status"] = "no_changes_needed"

        return result

    def _get_current_version(self) -> str:
        """Get current system version from VERSION file."""
        version_file = self.working_dir / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip()
        return "0.0.0.0"

    def backup_before_migration(self) -> Path:
        """Create backup before starting migration.

        Returns:
            Path to created backup directory
        """
        backup_dir = self.working_dir / f"backups/migrate-{self._get_current_version()}-to-"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Copy important directories
        dirs_to_backup = ["config", "data", "logs", "storage"]
        for dir_name in dirs_to_backup:
            src = self.working_dir / dir_name
            if src.exists():
                dst = backup_dir / dir_name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

        return backup_dir