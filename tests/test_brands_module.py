import pytest
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest


class TestBrandsModuleCatalog:
    def test_brands_in_bundled(self):
        assert "brands" in BUNDLED

    def test_brands_in_bundled_labels(self):
        assert "brands" in BUNDLED_LABELS
        assert BUNDLED_LABELS["brands"] == "Бренди"

    def test_brands_manifest_loads(self):
        manifest = bundled_manifest("brands")
        assert manifest is not None
        assert manifest.module.name == "brands"
        assert manifest.module.kind == "application"
        assert manifest.module.namespace == "brands"
        assert manifest.module.ui is not None
        assert manifest.module.ui.entry == "index.html"
        assert manifest.module.database is not None
        assert manifest.module.database.schema == "brands"
        assert manifest.module.database.migrations_dir == "migrations"
        assert manifest.module.runtime is not None
        assert manifest.module.runtime.gpu is False
        assert manifest.module.health_check is not None
        assert manifest.module.health_check.path == "/health"
        assert manifest.module.lifecycle is not None


class TestBrandsModuleLifecycle:
    @pytest.mark.asyncio
    async def test_brands_install_activate(self):
        from core.module.lifecycle import ModuleManager
        from core.module.manifest import ModuleManifest
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/brands/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest = ModuleManifest.from_yaml(manifest_path.read_text(encoding="utf-8"))
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("brands", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("brands")
            assert result["status"] == "active"


class TestBrandsModuleCompatibility:
    def test_brands_no_gpu_required(self):
        manifest = bundled_manifest("brands")
        assert manifest.module.runtime.gpu is False

    def test_brands_schema_version_2(self):
        manifest = bundled_manifest("brands")
        assert manifest.module.schema_version == "2.0"