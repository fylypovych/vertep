import pytest
import yaml
from pathlib import Path
from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest


class TestMonitoringModuleCatalog:
    def test_monitoring_in_bundled(self):
        assert "monitoring" in BUNDLED

    def test_monitoring_in_bundled_labels(self):
        assert "monitoring" in BUNDLED_LABELS
        assert BUNDLED_LABELS["monitoring"] == "Моніторинг"

    def test_monitoring_manifest_loads(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert manifest is not None
        assert manifest.name == "monitoring"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "monitoring"
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


class TestMonitoringModuleLifecycle:
    @pytest.mark.asyncio
    async def test_monitoring_install_activate(self):
        from core.module.lifecycle import ModuleManager
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mgr = ModuleManager(Path(tmp))
            manifest_path = Path("modules/monitoring/manifest.yaml")
            assert manifest_path.exists(), "manifest.yaml not found"
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = ModuleManifest.from_dict(manifest_data)
            pkg = {"manifest": manifest, "version": "1.0.0", "sha256": "test"}
            result = await mgr.install("monitoring", pkg)
            assert result["status"] == "installed"
            result = await mgr.activate("monitoring")
            assert result["status"] == "active"


class TestMonitoringModuleCompatibility:
    def test_monitoring_no_gpu_required(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert manifest.placement["requires_gpu"] is False

    def test_monitoring_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert manifest.schema_version == "2.0"

    def test_monitoring_provides_capabilities(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert "monitoring" in manifest.capabilities
        assert "metrics" in manifest.capabilities
        assert "alerting" in manifest.capabilities