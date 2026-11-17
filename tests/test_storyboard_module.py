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


class TestStoryboardModuleLifecycle:
    @pytest.mark.asyncio
    async def test_storyboard_install_activate(self):
        from core.module.lifecycle import ModuleManager
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/storyboard/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = ModuleManifest.from_dict(manifest_data)
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("storyboard", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("storyboard")
            assert result["status"] == "active"


class TestStoryboardModuleCompatibility:
    def test_storyboard_no_gpu_required(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest.placement["requires_gpu"] is False

    def test_storyboard_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert manifest.schema_version == "2.0"

    def test_storyboard_depends_on_characters_brands(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("storyboard"))
        assert "characters" in manifest.dependencies
        assert "brands" in manifest.dependencies