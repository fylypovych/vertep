import pytest
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest


class TestMonitoringModuleCatalog:
    def test_monitoring_in_bundled(self):
        assert "monitoring" in BUNDLED

    def test_monitoring_in_bundled_labels(self):
        assert "monitoring" in BUNDLED_LABELS
        assert BUNDLED_LABELS["monitoring"] == "Моніторинг"

    def test_monitoring_manifest_loads(self):
        manifest = bundled_manifest("monitoring")
        assert manifest is not None
        assert manifest.module.name == "monitoring"
        assert manifest.module.kind == "application"
        assert manifest.module.namespace == "monitoring"
        assert manifest.module.ui is not None
        assert manifest.module.ui.entry == "index.html"
        assert manifest.module.database is not None
        assert manifest.module.database.schema == "monitoring"
        assert manifest.module.database.migrations_dir == "migrations"
        assert manifest.module.runtime is not None
        assert manifest.module.runtime.gpu is False
        assert manifest.module.health_check is not None
        assert manifest.module.health_check.path == "/health"
        assert manifest.module.lifecycle is not None


class TestMonitoringModuleLifecycle:
    @pytest.mark.asyncio
    async def test_monitoring_install_activate(self):
        from core.module.lifecycle import ModuleManager
        from core.module.manifest import ModuleManifest
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/monitoring/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest = ModuleManifest.from_yaml(manifest_path.read_text(encoding="utf-8"))
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("monitoring", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("monitoring")
            assert result["status"] == "active"


class TestMonitoringModuleCompatibility:
    def test_monitoring_no_gpu_required(self):
        manifest = bundled_manifest("monitoring")
        assert manifest.module.runtime.gpu is False

    def test_monitoring_schema_version_2(self):
        manifest = bundled_manifest("monitoring")
        assert manifest.module.schema_version == "2.0"

    def test_monitoring_provides_capabilities(self):
        manifest = bundled_manifest("monitoring")
        assert "monitoring" in manifest.module.provides_capabilities
        assert "metrics" in manifest.module.provides_capabilities
        assert "alerting" in manifest.module.provides_capabilities