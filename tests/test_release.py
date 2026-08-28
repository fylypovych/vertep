import importlib.util
from pathlib import Path

from core.app import app
from core.version import application_version


def load_release_module():
    path = Path("scripts/release.py").resolve()
    spec = importlib.util.spec_from_file_location("vertep_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_version_is_strictly_sequential():
    release = load_release_module()
    assert release.next_version([]) == "0.0.0.1"
    assert release.next_version(["v0.0.0.1"]) == "0.0.0.2"
    assert release.next_version(["v0.0.0.1", "0.0.0.3", "unrelated"]) == "0.0.0.4"


def test_known_versions_include_untagged_release_metadata(tmp_path, monkeypatch):
    release = load_release_module()
    (tmp_path / "VERSION").write_text("0.0.0.5\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.0.0.3 - 2026-08-25\n", encoding="utf-8"
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir()
    (release_dir / "0.0.0.4.md").write_text("release", encoding="utf-8")
    monkeypatch.setattr(release, "git", lambda *_args: "v0.0.0.1")

    versions = release.known_versions(tmp_path)

    assert set(versions) == {"v0.0.0.1", "0.0.0.3", "0.0.0.4", "0.0.0.5"}
    assert release.next_version(versions) == "0.0.0.6"


def test_release_moves_unreleased_notes_to_version_section():
    release = load_release_module()
    source = "# Changelog\n\n## Unreleased\n\n- Added feature.\n- Fixed bug.\n\n## Older\n\n- Old.\n"
    notes = release.unreleased_notes(source)
    result = release.release_changelog(source, "0.0.0.2", notes)
    assert notes == ["- Added feature.", "- Fixed bug."]
    assert "## Unreleased\n\n## 0.0.0.2" in result
    assert result.count("- Added feature.") == 1


def test_runtime_version_comes_from_version_file():
    assert app.version == application_version()
    assert application_version().startswith("0.0.0.")


def test_release_push_is_atomic_and_generates_notes():
    script = Path("scripts/release.py").read_text(encoding="utf-8")
    assert '"push", "--atomic"' in script
    assert "release_path.write_text" in script
    assert "scan_staged_secrets" in script
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "contents: write" in workflow
    assert "python scripts/release.py" in workflow
