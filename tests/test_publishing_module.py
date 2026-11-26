from pathlib import Path
import yaml
from core.module.catalog import BUNDLED_LABELS, RETIRED_BUNDLED
from core.module.lifecycle import BUNDLED, bundled_manifest
from core.module.manifest import ModuleManifest

APP_MANIFEST = Path(__file__).resolve().parents[1] / "modules" / "publisher" / "app" / "manifest.yaml"


class TestPublishingModuleCatalog:
    def test_single_publisher_module_in_bundled_and_publishing_retired(self):
        assert "publisher" in BUNDLED
        assert "publishing" not in BUNDLED
        assert "publishing" in RETIRED_BUNDLED

    def test_publisher_in_bundled_labels(self):
        assert BUNDLED_LABELS["publisher"] == "Публікація"
        assert "publishing" not in BUNDLED_LABELS

    def test_bundled_publisher_declares_published_navigation(self):
        manifest = ModuleManifest.from_dict(bundled_manifest("publisher"))
        assert manifest.name == "publisher"
        assert manifest.ui is not None
        assert any(entry.get("route") == "/published" for entry in manifest.ui["navigation"])

    def test_publisher_app_manifest_loads(self):
        manifest = ModuleManifest.from_dict(yaml.safe_load(APP_MANIFEST.read_text(encoding="utf-8")))
        assert manifest is not None
        assert manifest.name == "publisher"
        assert manifest.kind == "application"
        assert manifest.api is not None
        assert manifest.api["namespace"] == "publisher"
        assert {"/publish", "/publications", "/publications/{job_id}/publish"} <= set(manifest.api["routes"])
        assert manifest.ui is not None
        assert manifest.ui["entry"] == "index.html"
        assert manifest.resource_requirements is not None
        assert manifest.placement["requires_gpu"] is False
        assert manifest.health is not None
        assert manifest.health.get("timeout_seconds") is not None


class TestPublishingModuleCompatibility:
    def test_publisher_schema_version_2(self):
        manifest = ModuleManifest.from_dict(yaml.safe_load(APP_MANIFEST.read_text(encoding="utf-8")))
        assert manifest.schema_version == "2.0"

    def test_publisher_has_no_database_and_declares_permissions(self):
        manifest = ModuleManifest.from_dict(yaml.safe_load(APP_MANIFEST.read_text(encoding="utf-8")))
        assert manifest.migrations == [] and manifest.dependencies == []
        assert manifest.permissions == ["publishing.read", "publishing.publish"]
        assert not (Path(__file__).resolve().parents[1] / "modules" / "publishing").exists()
