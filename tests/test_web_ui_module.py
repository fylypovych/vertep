import pytest
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest


class TestWebUIModuleCatalog:
    def test_web_ui_in_bundled(self):
        assert "web-ui" in BUNDLED

    def test_web_ui_in_bundled_labels(self):
        assert "web-ui" in BUNDLED_LABELS
        assert BUNDLED_LABELS["web-ui"] == "Web UI"

    def test_web_ui_manifest_loads(self):
        manifest = bundled_manifest("web-ui")
        assert manifest is not None
        assert manifest.module.name == "web-ui"
        assert manifest.module.kind == "application"
        assert manifest.module.namespace == "web-ui"
        assert manifest.module.ui is not None
        assert manifest.module.ui.entry == "index.html"
        assert manifest.module.database is not None
        assert manifest.module.database.schema == "web_ui"
        assert manifest.module.database.migrations_dir == "migrations"
        assert manifest.module.runtime is not None
        assert manifest.module.runtime.gpu is False
        assert manifest.module.health_check is not None
        assert manifest.module.health_check.path == "/health"
        assert manifest.module.lifecycle is not None


class TestWebUIModuleLifecycle:
    @pytest.mark.asyncio
    async def test_web_ui_install_activate(self):
        from core.module.lifecycle import ModuleManager
        from core.module.manifest import ModuleManifest
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/web-ui/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest = ModuleManifest.from_yaml(manifest_path.read_text(encoding="utf-8"))
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("web-ui", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("web-ui")
            assert result["status"] == "active"


class TestWebUIModuleCompatibility:
    def test_web_ui_no_gpu_required(self):
        manifest = bundled_manifest("web-ui")
        assert manifest.module.runtime.gpu is False

    def test_web_ui_schema_version_2(self):
        manifest = bundled_manifest("web-ui")
        assert manifest.module.schema_version == "2.0"

    def test_web_ui_depends_on_all_modules(self):
        manifest = bundled_manifest("web-ui")
        assert "characters" in manifest.module.dependencies
        assert "brands" in manifest.module.dependencies
        assert "storyboard" in manifest.module.dependencies
        assert "publishing" in manifest.module.dependencies
        assert "monitoring" in manifest.module.dependencies

    def test_web_ui_provides_web_ui_capability(self):
        manifest = bundled_manifest("web-ui")
        assert "web_ui" in manifest.module.provides_capabilities