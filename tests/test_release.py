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
    assert release.next_version(["0.0.0.99"]) == "0.0.1.0"
    assert release.next_version(["0.0.99.99"]) == "0.1.0.0"


def test_known_versions_include_untagged_release_metadata(tmp_path, monkeypatch):
    release = load_release_module()
    (tmp_path / "VERSION").write_text("0.0.0.5\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.0.0.3 - 2026-08-25\n", encoding="utf-8"
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir()
    (release_dir / "0.0.0.4.md").write_text("release", encoding="utf-8")
    monkeypatch.setattr(release, "git", lambda _root, *args: (
        "v0.0.0.1" if args[:2] == ("tag", "--list") else "0.0.0.2"
    ))

    versions = release.known_versions(tmp_path)

    assert set(versions) == {"v0.0.0.1", "0.0.0.2", "0.0.0.3", "0.0.0.4", "0.0.0.5"}
    assert release.next_version(versions) == "0.0.0.6"


def test_release_moves_unreleased_notes_to_version_section():
    release = load_release_module()
    source = "# Changelog\n\n## Unreleased\n\n- Додано функцію.\n- Виправлено помилку.\n\n## Older\n\n- Old.\n"
    notes = release.unreleased_notes(source)
    result = release.release_changelog(source, "0.0.0.2", notes)
    assert notes == ["- Додано функцію.", "- Виправлено помилку."]
    assert "## Unreleased\n\n## 0.0.0.2" in result
    assert result.count("- Додано функцію.") == 1


def test_release_descriptions_must_be_ukrainian():
    release = load_release_module()
    release.require_ukrainian("Виправлено процес оновлення", "Опис")
    try:
        release.require_ukrainian("Fix release process", "Опис")
    except RuntimeError as error:
        assert "українською" in str(error)
    else:
        raise AssertionError("English-only release descriptions must be rejected")


def test_runtime_version_comes_from_version_file():
    assert app.version == application_version()
    assert application_version().startswith("0.0.0.")


def test_release_is_prepared_once_and_ci_never_commits_to_main():
    script = Path("scripts/release.py").read_text(encoding="utf-8")
    assert 'git(root, "commit", "-m", f"{version} — {title}")' in script
    assert "release_path.write_text" in script
    assert "scan_staged_secrets" in script
    assert 'git(root, "add", "-A")' not in script
    assert 'git(root, "add", "-u")' in script
    assert '"--untracked-files=no"' in script
    assert 'git(root, "tag", "-a"' not in script
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "contents: write" in workflow
    assert "python scripts/release.py --check" in workflow
    assert "git commit" not in workflow
    assert "HEAD:main" not in workflow
    assert 'git push origin "refs/tags/$version"' in workflow
