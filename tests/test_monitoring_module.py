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


class TestMonitoringModuleCompatibility:
    def test_monitoring_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert manifest.schema_version == "2.0"

    def test_monitoring_provides_capabilities(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("monitoring"))
        assert "monitoring" in manifest.capabilities
        assert "metrics" in manifest.capabilities
        assert "alerting" in manifest.capabilities
