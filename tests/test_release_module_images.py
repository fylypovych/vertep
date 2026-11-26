"""Owner decision 19.11.2026: the release builds module images before CORE and
CORE's bundled manifests run exactly those images (by sha256 digest)."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "ghcr.io/owner/vertep-module-characters@sha256:" + "ab" * 32


def test_bundled_manifest_uses_the_image_pinned_by_the_release(tmp_path, monkeypatch):
    from core.module import lifecycle
    from core.module.manifest import ModuleManifest
    images = tmp_path / "module-images.json"
    images.write_text(json.dumps({"characters": DIGEST, "monitoring": "not-pinned:latest"}))
    monkeypatch.setattr(lifecycle, "MODULE_IMAGES", images)
    assert ModuleManifest.from_dict(lifecycle.bundled_manifest("characters")).container["image"] == DIGEST
    # An unpinned reference is ignored: the declared image stays.
    declared = yaml.safe_load((ROOT / "modules/monitoring/manifest.yaml").read_text())["container"]["image"]
    assert lifecycle.bundled_manifest("monitoring")["container"]["image"] == declared
    monkeypatch.setattr(lifecycle, "MODULE_IMAGES", tmp_path / "absent.json")
    assert lifecycle.bundled_manifest("characters")["container"]["image"].startswith("registry.example/")


def test_release_builds_module_images_before_core_and_pins_them():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    modules = jobs["build-module-images"]
    assert set(modules["strategy"]["matrix"]["name"]) == {"characters", "monitoring", "brands", "storyboard"}
    for name in modules["strategy"]["matrix"]["name"]:
        assert (ROOT / "modules" / name / "Dockerfile").is_file()
    assert "build-module-images" in jobs["build-images"]["needs"]
    steps = [step.get("name", "") for step in jobs["build-images"]["steps"]]
    assert steps.index("Pin module images into the CORE build context") < steps.index("Build and publish ${{ matrix.name }}")
    assert "COPY modules modules" in (ROOT / "Dockerfile").read_text()  # the CORE image carries bundled modules
    assert "config/module-images.json" in (ROOT / ".gitignore").read_text()
