import importlib.util
import json
from pathlib import Path


def module():
    path = Path(__file__).parents[1] / "scripts" / "generate-sbom.py"
    spec = importlib.util.spec_from_file_location("generate_sbom", path)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(value)
    return value


def test_sbom_contains_python_dependencies_and_pinned_images(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "requirements.txt").write_text("fastapi==0.115.6\ncryptography>=41.0.7,<47\n",
                                            encoding="utf-8")
    lock = tmp_path / "images.json"
    lock.write_text(json.dumps({"core": {
        "reference": "registry.vertep.ai/vertep/core",
        "digest": "sha256:" + "a" * 64,
        "platforms": ["linux/amd64", "linux/arm64"],
    }}), encoding="utf-8")
    sbom = module().generate(root, lock, "0.0.0.12")
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["metadata"]["component"]["version"] == "0.0.0.12"
    assert any(item.get("purl") == "pkg:pypi/fastapi@0.115.6" for item in sbom["components"])
    container = next(item for item in sbom["components"] if item["type"] == "container")
    assert container["hashes"][0]["content"] == "a" * 64
