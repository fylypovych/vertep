from core.module.catalog import BUNDLED_LABELS
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest


class TestWebUIModuleCatalog:
    def test_web_ui_in_bundled(self):
        assert "web-ui" in BUNDLED

    def test_web_ui_in_bundled_labels(self):
        assert "web-ui" in BUNDLED_LABELS
        assert BUNDLED_LABELS["web-ui"] == "Web UI"

    def test_web_ui_manifest_loads(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("web-ui"))
        assert manifest is not None
        assert manifest.name == "web-ui"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "web-ui"
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


class TestWebUIModuleCompatibility:
    def test_web_ui_schema_version_2(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("web-ui"))
        assert manifest.schema_version == "2.0"

    def test_web_ui_depends_on_all_modules(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("web-ui"))
        assert "characters" in manifest.dependencies
        assert "brands" in manifest.dependencies
        assert "storyboard" in manifest.dependencies
        assert "publishing" in manifest.dependencies
        assert "monitoring" in manifest.dependencies

    def test_web_ui_provides_web_ui_capability(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("web-ui"))
        assert "web_ui" in manifest.capabilities
