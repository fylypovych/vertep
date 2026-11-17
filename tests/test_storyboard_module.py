import pytest
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest


class TestStoryboardModuleCatalog:
    def test_storyboard_in_bundled(self):
        assert "storyboard" in BUNDLED

    def test_storyboard_in_bundled_labels(self):
        assert "storyboard" in BUNDLED_LABELS
        assert BUNDLED_LABELS["storyboard"] == "Сторіборд"

    def test_storyboard_manifest_loads(self):
        manifest = bundled_manifest("storyboard")
        assert manifest is not None
        assert manifest.module.name == "storyboard"
        assert manifest.module.kind == "application"
        assert manifest.module.namespace == "storyboard"
        assert manifest.module.ui is not None
        assert manifest.module.ui.entry == "index.html"
        assert manifest.module.database is not None
        assert manifest.module.database.schema == "storyboard"
        assert manifest.module.database.migrations_dir == "migrations"
        assert manifest.module.runtime is not None
        assert manifest.module.runtime.gpu is False
        assert manifest.module.health_check is not None
        assert manifest.module.health_check.path == "/health"
        assert manifest.module.lifecycle is not None


class TestStoryboardModuleLifecycle:
    @pytest.mark.asyncio
    async def test_storyboard_install_activate(self):
        from core.module.lifecycle import ModuleManager
        from core.module.manifest import ModuleManifest
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/storyboard/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest = ModuleManifest.from_yaml(manifest_path.read_text(encoding="utf-8"))
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("storyboard", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("storyboard")
            assert result["status"] == "active"


class TestStoryboardModuleCompatibility:
    def test_storyboard_no_gpu_required(self):
        manifest = bundled_manifest("storyboard")
        assert manifest.module.runtime.gpu is False

    def test_storyboard_schema_version_2(self):
        manifest = bundled_manifest("storyboard")
        assert manifest.module.schema_version == "2.0"

    def test_storyboard_depends_on_characters_brands(self):
        manifest = bundled_manifest("storyboard")
        assert "characters" in manifest.module.dependencies
        assert "brands" in manifest.module.dependencies