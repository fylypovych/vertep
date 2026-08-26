import importlib.util
import json
import os
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "release-layout.py"
    spec = importlib.util.spec_from_file_location("release_layout", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _payload(path: Path, version: str, content: str = "content") -> Path:
    path.mkdir()
    (path / "VERSION").write_text(version, encoding="utf-8")
    (path / "application.txt").write_text(content, encoding="utf-8")
    return path


def test_release_activation_is_atomic_and_rollback_restores_previous(tmp_path):
    layout = _module()
    base = tmp_path / "appliance"
    base.mkdir()
    (base / ".env").write_text("SECRET=value\n", encoding="utf-8")
    first = layout.prepare(base, _payload(tmp_path / "one", "1.0.0"), "1.0.0")
    layout.activate(base, "1.0.0")
    second = layout.prepare(base, _payload(tmp_path / "two", "1.1.0", "new"), "1.1.0")
    layout.activate(base, "1.1.0")

    assert os.readlink(base / "current") == "releases/1.1.0"
    assert (base / "current" / "application.txt").read_text() == "new"
    assert (first / ".env").is_symlink() and (second / ".env").is_symlink()
    assert json.loads((second / ".release.json").read_text())["version"] == "1.1.0"

    layout.rollback(base)
    assert os.readlink(base / "current") == "releases/1.0.0"
    assert (base / "current" / "application.txt").read_text() == "content"


def test_release_payload_rejects_symlinks_and_existing_version(tmp_path):
    layout = _module()
    base = tmp_path / "appliance"
    base.mkdir()
    payload = _payload(tmp_path / "payload", "2.0.0")
    (payload / "escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        layout.prepare(base, payload, "2.0.0")

    (payload / "escape").unlink()
    layout.prepare(base, payload, "2.0.0")
    with pytest.raises(FileExistsError):
        layout.prepare(base, payload, "2.0.0")


def test_prune_preserves_active_previous_and_retention_window(tmp_path):
    layout = _module()
    base = tmp_path / "appliance"
    base.mkdir()
    (base / ".env").write_text("x=1\n")
    for index in range(1, 6):
        version = f"1.0.{index}"
        layout.prepare(base, _payload(tmp_path / f"payload-{index}", version), version)
        layout.activate(base, version)
    removed = layout.prune(base, keep=2)
    assert removed == ["1.0.3", "1.0.2", "1.0.1"]
    assert (base / "releases/1.0.5").is_dir()
    assert (base / "releases/1.0.4").is_dir()
    with pytest.raises(ValueError, match="at least two"):
        layout.prune(base, keep=1)
