"""Migration step from 0.0.2.X to 0.0.3.0.

Handles configuration updates, schema changes, and data migration
for the Architecture 2.0 transition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from core.migration.manager import MigrationManager


class MigrationStep:
    """A single migration step between versions."""

    def __init__(
        self, current_version: str, target_version: str, working_dir: Path
    ) -> None:
        self.current_version = current_version
        self.target_version = target_version
        self.working_dir = working_dir
        self.new_version = target_version

    def is_applicable(self) -> bool:
        """Check if this migration step applies to the current state."""
        # Apply when moving from 0.0.2.X to 0.0.3.0
        current_parts = [int(x) for x in self.current_version.split(".")]
        target_parts = [int(x) for x in self.target_version.split(".")]
        return (
            current_parts[0] == 0
            and current_parts[1] == 0
            and current_parts[2] >= 2
            and target_parts[0] == 0
            and target_parts[1] == 0
            and target_parts[2] == 3
        )

    def apply(self) -> List[str]:
        """Apply the migration changes.

        Returns:
            List of changed file paths
        """
        changed: List[str] = []
        core_dir = self.working_dir / "core"

        # Update VERSION file
        version_file = self.working_dir / "VERSION"
        if version_file.exists():
            old_version = version_file.read_text(encoding="utf-8").strip()
            version_file.write_text(self.target_version + "\n", encoding="utf-8")
            changed.append("VERSION")
            print(f"Updated VERSION: {old_version} → {self.target_version}")

        # Update CHANGELOG.md header if needed
        changelog = self.working_dir / "CHANGELOG.md"
        if changelog.exists():
            content = changelog.read_text(encoding="utf-8")
            # Add new unreleased section if not present
            if "## Unreleased" not in content:
                # Insert at top after first line
                lines = content.split("\n", 1)
                if len(lines) > 1:
                    lines[1] = "## Unreleased\n\n" + lines[1]
                    changelog.write_text("\n".join(lines), encoding="utf-8")
                    changed.append("CHANGELOG.md")

        # Update core module files as needed
        # Example: update workflow engine configurations
        engine_file = core_dir / "workflow" / "engine.py"
        if engine_file.exists():
            # No changes needed for this step, but could add version checks
            pass

        return changed