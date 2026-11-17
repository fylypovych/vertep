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


class TestBrandsModuleLifecycle:
    @pytest.mark.asyncio
    async def test_brands_install_activate(self):
        from core.module.lifecycle import ModuleManager
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/brands/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = ModuleManifest.from_dict(manifest_data)
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("brands", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("brands")
            assert result["status"] == "active"


class TestBrandsModuleCompatibility:
    def test_brands_no_gpu_required(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("brands"))
        assert manifest.placement["requires_gpu"] is False

    def test_brands_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("brands"))
        assert manifest.schema_version == "2.0"