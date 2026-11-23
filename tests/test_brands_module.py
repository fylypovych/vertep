import pytest
import yaml
from pathlib import Path
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest


class TestBrandsModuleCatalog:
    def test_brands_in_bundled(self):
        assert "brands" in BUNDLED

    def test_brands_in_bundled_labels(self):
        assert "brands" in BUNDLED_LABELS
        assert BUNDLED_LABELS["brands"] == "Бренди"

    def test_brands_manifest_loads(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("brands"))
        assert manifest is not None
        assert manifest.name == "brands"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "brands"
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


# Install/activate with the module's own schema, the CORE brand API through the
# module, migration and permissions: tests/test_brands_module_e2e.py.


class TestBrandsModuleCompatibility:
    def test_brands_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("brands"))
        assert manifest.schema_version == "2.0"
