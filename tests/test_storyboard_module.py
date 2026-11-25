import pytest
import yaml
from pathlib import Path
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest


class TestStoryboardModuleCatalog:
    def test_storyboard_in_bundled(self):
        assert "storyboard" in BUNDLED

    def test_storyboard_in_bundled_labels(self):
        assert "storyboard" in BUNDLED_LABELS
        assert BUNDLED_LABELS["storyboard"] == "Сторіборд"

    def test_storyboard_manifest_loads(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest is not None
        assert manifest.name == "storyboard"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "storyboard"
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


# Install/activate without a database and reading job storyboards through CORE:
# tests/test_storyboard_module_e2e.py.


class TestStoryboardModuleCompatibility:
    def test_storyboard_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest.schema_version == "2.0"

    def test_storyboard_declares_scenario_library_migration(self):
        """#508: Storyboard owns the Scenario Library in its own PostgreSQL schema;
        Job Storyboards remain owned by CORE (#501)."""
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest.migrations == [{"id": "m001_initial", "version": "1.0.0"}]
        assert manifest.permissions == ["storyboard.read"]
        migration = Path("modules/storyboard/migrations/m001_initial.sql")
        assert migration.is_file()
        ddl = migration.read_text(encoding="utf-8")
        for table in ("scenarios", "scenario_versions", "scenario_shares", "scenario_audit"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
