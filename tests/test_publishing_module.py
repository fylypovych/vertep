import pytest
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest


class TestPublishingModuleCatalog:
    def test_publishing_in_bundled(self):
        assert "publishing" in BUNDLED

    def test_publishing_in_bundled_labels(self):
        assert "publishing" in BUNDLED_LABELS
        assert BUNDLED_LABELS["publishing"] == "Публікація"

    def test_publishing_manifest_loads(self):
        manifest = bundled_manifest("publishing")
        assert manifest is not None
        assert manifest.module.name == "publishing"
        assert manifest.module.kind == "application"
        assert manifest.module.namespace == "publishing"
        assert manifest.module.ui is not None
        assert manifest.module.ui.entry == "index.html"
        assert manifest.module.database is not None
        assert manifest.module.database.schema == "publishing"
        assert manifest.module.database.migrations_dir == "migrations"
        assert manifest.module.runtime is not None
        assert manifest.module.runtime.gpu is False
        assert manifest.module.health_check is not None
        assert manifest.module.health_check.path == "/health"
        assert manifest.module.lifecycle is not None


class TestPublishingModuleLifecycle:
    @pytest.mark.asyncio
    async def test_publishing_install_activate(self):
        from core.module.lifecycle import ModuleManager
        from core.module.manifest import ModuleManifest
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/publishing/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest = ModuleManifest.from_yaml(manifest_path.read_text(encoding="utf-8"))
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("publishing", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("publishing")
            assert result["status"] == "active"


class TestPublishingModuleCompatibility:
    def test_publishing_no_gpu_required(self):
        manifest = bundled_manifest("publishing")
        assert manifest.module.runtime.gpu is False

    def test_publishing_schema_version_2(self):
        manifest = bundled_manifest("publishing")
        assert manifest.module.schema_version == "2.0"

    def test_publishing_depends_on_storyboard_brands(self):
        manifest = bundled_manifest("publishing")
        assert "storyboard" in manifest.module.dependencies
        assert "brands" in manifest.module.dependencies

    def test_publishing_provides_publishing_capability(self):
        manifest = bundled_manifest("publishing")
        assert "publishing" in manifest.module.provides_capabilities