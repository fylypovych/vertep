import importlib.util
import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="immutable appliance activation requires POSIX symlinks"
)


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


def test_discard_removes_only_a_stale_release_left_by_a_failed_attempt(tmp_path):
    """#401 F1: a failed apply rolls back but keeps its prepared tree; the retry of the
    same version must be possible, while the active and rollback releases stay protected."""
    layout = _module()
    base = tmp_path / "appliance"
    base.mkdir()
    (base / ".env").write_text("x=1\n")
    for version in ("1.0.0", "1.0.1"):
        layout.prepare(base, _payload(tmp_path / f"payload-{version}", version), version)
        layout.activate(base, version)
    layout.rollback(base)  # 1.0.1 failed: 1.0.0 is active again, 1.0.1 stays on disk
    assert (base / "releases" / "1.0.1").is_dir()

    with pytest.raises(RuntimeError, match="active"):
        layout.discard(base, "1.0.0")
    assert layout.discard(base, "1.0.1") is True
    assert not (base / "releases" / "1.0.1").exists()
    assert layout.discard(base, "1.0.1") is False
    layout.prepare(base, _payload(tmp_path / "payload-retry", "1.0.1"), "1.0.1")
    layout.activate(base, "1.0.1")
    with pytest.raises(RuntimeError, match="rollback target"):
        layout.discard(base, "1.0.0")


def test_a_legacy_installation_with_rendered_storage_is_rescued_as_a_release(tmp_path):
    """A flat installation that already rendered a video carries its storage (with the
    ``final/video.mp4`` pointer link) in the legacy payload. That data is not release
    code: the rescue links the shared storage instead of refusing the whole update."""
    layout = _module()
    base = tmp_path / "appliance"
    (base / "storage" / "jobs" / "job-1" / "final").mkdir(parents=True)
    final = base / "storage" / "jobs" / "job-1" / "final"
    (final / "video_v1.mp4").write_bytes(b"render")
    (final / "video.mp4").symlink_to("video_v1.mp4")
    legacy = _payload(tmp_path / "legacy", "1.0.0")
    (legacy / "storage" / "jobs" / "job-1" / "final").mkdir(parents=True)
    (legacy / "storage" / "jobs" / "job-1" / "final" / "video.mp4").symlink_to("video_v1.mp4")

    release = layout.prepare(base, legacy, "1.0.0")
    layout.activate(base, "1.0.0")
    assert (release / "storage").is_symlink()
    assert (base / "current" / "storage" / "jobs" / "job-1" / "final" / "video.mp4").read_bytes() \
        == b"render"
    assert layout.tree_digest(legacy) == layout.tree_digest(_payload(tmp_path / "bare", "1.0.0"))


def test_rollback_before_any_activation_leaves_a_flat_installation_unchanged(tmp_path):
    layout = _module()
    base = tmp_path / "appliance"
    base.mkdir()
    (base / "VERSION").write_text("1.0.0", encoding="utf-8")
    layout.rollback(base)
    assert not (base / "current").exists() and (base / "VERSION").read_text() == "1.0.0"
