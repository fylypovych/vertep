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

    def test_storyboard_has_no_database_of_its_own(self):
        """#501 owner decision: storyboards stay in the jobs; the module reads them."""
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest.migrations == [] and manifest.permissions == ["storyboard.read"]
        assert not Path("modules/storyboard/migrations").exists() or not any(Path("modules/storyboard/migrations").iterdir())
