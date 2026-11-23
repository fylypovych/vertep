import pytest
import yaml
from pathlib import Path
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest


class TestPublishingModuleCatalog:
    def test_publishing_in_bundled(self):
        assert "publishing" in BUNDLED

    def test_publishing_in_bundled_labels(self):
        assert "publishing" in BUNDLED_LABELS
        assert BUNDLED_LABELS["publishing"] == "Публікація"

    def test_publishing_manifest_loads(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert manifest is not None
        assert manifest.name == "publishing"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "publishing"
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


# Lifecycle without a database, the verified user context and the Worker send path:
# tests/test_publishing_module_e2e.py.


class TestPublishingModuleCompatibility:
    def test_publishing_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert manifest.schema_version == "2.0"

    def test_publishing_has_no_database_channels_or_send_path(self):
        """#502 owner decision: interface and editor actions only."""
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert manifest.migrations == [] and manifest.dependencies == []
        assert manifest.permissions == ["publishing.read", "publishing.publish"]
        assert not Path("modules/publishing/publishing_logic.py").exists()
