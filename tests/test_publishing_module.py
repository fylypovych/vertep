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


class TestPublishingModuleLifecycle:
    @pytest.mark.asyncio
    async def test_publishing_install_activate(self):
        from core.module.lifecycle import ModuleManager
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/publishing/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = ModuleManifest.from_dict(manifest_data)
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("publishing", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("publishing")
            assert result["status"] == "active"


class TestPublishingModuleCompatibility:
    def test_publishing_no_gpu_required(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert manifest.placement["requires_gpu"] is False

    def test_publishing_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert manifest.schema_version == "2.0"

    def test_publishing_depends_on_storyboard_brands(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert "storyboard" in manifest.dependencies
        assert "brands" in manifest.dependencies

    def test_publishing_provides_publishing_capability(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publishing"))
        assert "publishing" in manifest.capabilities