import importlib.util
import json
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


def test_known_versions_with_both_formats(tmp_path, monkeypatch):
    release = load_release_module()
    (tmp_path / "VERSION").write_text("0.0.0.5\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.0.0.3 - 2026-08-25\n"
        "## ПРАВИЛЬНА НАЗВА: 0.0.0.4\n\n- Виправлено помилку.\n\n"
        "## ПРАВИЛЬНА НАЗВА: 0.0.0.6\n\n- Додано функцію.\n",
        encoding="utf-8"
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir(exist_ok=True)
    (release_dir / "0.0.0.2.md").write_text("release", encoding="utf-8")
    monkeypatch.setattr(release, "git", lambda _root, *args: (
        "v0.0.0.1" if args[:2] == ("tag", "--list") else "0.0.0.7"
    ))

    versions = release.known_versions(tmp_path)

    assert set(versions) == {"v0.0.0.1", "0.0.0.2", "0.0.0.3", "0.0.0.4", "0.0.0.5", "0.0.0.6", "0.0.0.7"}
    assert release.next_version(versions) == "0.0.0.8"


def test_release_moves_unreleased_notes_to_version_section():
    release = load_release_module()
    source = "# Changelog\n\n## Unreleased\n\n- Додано функцію.\n- Виправлено помилку.\n\n## Older\n\n- Old.\n"
    notes = release.unreleased_notes(source)
    result = release.release_changelog(source, "0.0.0.2", notes)
    assert notes == ["- Додано функцію.", "- Виправлено помилку."]
    assert "## Unreleased\n\n## ПРАВИЛЬНА НАЗВА: 0.0.0.2" in result
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
    assert 'git(root, "commit", "-m", version)' in script
    assert "release_path.write_text" in script
    assert "scan_staged_secrets" in script
    assert 'git(root, "add", "-A")' not in script
    assert 'git(root, "add", "-u")' in script
    assert '"--untracked-files=no"' in script
    assert 'git(root, "tag", "-a"' not in script
    assert "--title" not in script
    assert 'git(root, "push", "origin", "main")' in script
    assert "--ignore=tests/test_browser_e2e.py" in script
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "contents: write" in workflow
    assert "python scripts/release.py --check" in workflow
    assert "git commit" not in workflow
    assert "HEAD:main" not in workflow
    assert 'git push origin "refs/tags/$version"' in workflow
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" "$remote_main"' in workflow


def test_release_changelog_uses_correct_format():
    release = load_release_module()
    changelog = "# Changelog\n\n## Unreleased\n\n- Додано функцію.\n- Виправлено помилку.\n"
    notes = ["- Додано функцію.", "- Виправлено помилку."]
    result = release.release_changelog(changelog, "0.0.0.96", notes)
    assert "## ПРАВИЛЬНА НАЗВА: 0.0.0.96" in result
    assert "- Додано функцію." in result
    assert "- Виправлено помилку." in result
    assert "## 0.0.0.96 -" not in result


def test_check_release_validates_first_line_of_release_notes(tmp_path, monkeypatch):
    release = load_release_module()
    version = "0.0.0.99"
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Тестова зміна.\n",
        encoding="utf-8",
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir(exist_ok=True)
    (release_dir / f"{version}.md").write_text(
        f"# Vertep {version}\n\n- Тестова зміна.\n", encoding="utf-8",
    )
    monkeypatch.setattr(release, "git", lambda _root, *args: (
        "0.0.0.99" if args == ("log", "-1", "--pretty=%s") else
        "" if args[:2] == ("status", "--porcelain") else
        ""
    ))
    assert release.check_release(tmp_path) == version


def test_check_release_rejects_wrong_first_line_in_release_notes(tmp_path, monkeypatch):
    release = load_release_module()
    version = "0.0.0.99"
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Тестова зміна.\n",
        encoding="utf-8",
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir(exist_ok=True)
    (release_dir / f"{version}.md").write_text(
        f"# Release {version}\n\n- Тестова зміна.\n", encoding="utf-8",
    )
    monkeypatch.setattr(release, "git", lambda _root, *args: (
        version if args == ("log", "-1", "--pretty=%s") else
        "" if args[:2] == ("status", "--porcelain") else
        ""
    ))
    try:
        release.check_release(tmp_path)
        raise AssertionError("Версія в реліз-нотах має перевірятися строго")
    except RuntimeError as error:
        assert "неправильну версію" in str(error)


def test_check_release_does_not_modify_repository(tmp_path, monkeypatch):
    release = load_release_module()
    version = "0.0.0.99"
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Тестова зміна.\n",
        encoding="utf-8",
    )
    release_dir = tmp_path / "releases"
    release_dir.mkdir(exist_ok=True)
    (release_dir / f"{version}.md").write_text(
        f"# Vertep {version}\n\n- Тестова зміна.\n", encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(release, "git", lambda _root, *args: calls.append(args) or (
        version if args == ("log", "-1", "--pretty=%s") else ""
    ))
    release.check_release(tmp_path)
    for call in calls:
        assert call[0] != "commit"
        assert call[0] != "add"
        assert call[0] != "push"
        assert call[0] != "tag"


def test_push_creates_release_notes_file_with_strict_header(tmp_path, monkeypatch):
    release = load_release_module()
    version = "0.0.0.97"
    notes = ["- Додано перевірку release notes."]
    (root_for_test := tmp_path).joinpath  # avoid unused
    target_root = tmp_path
    (target_root / "VERSION").write_text("0.0.0.96\n", encoding="utf-8")
    (target_root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n" + "\n".join(notes) + "\n\n",
        encoding="utf-8",
    )
    (target_root / "releases").mkdir()
    (target_root / "releases" / "0.0.0.96.md").write_text(
        "# Vertep 0.0.0.96\n\n", encoding="utf-8",
    )
    captured = {}
    state = {"committed_subject": "0.0.0.96"}

    def fake_git(r, *args):
        captured.setdefault("calls", []).append(args)
        if args[:1] == ("status",):
            return "" if args[1:3] == ("--porcelain", "--untracked-files=no") else ""
        if args[:1] == ("fetch",):
            return ""
        if args[:1] == ("log",) and (
            args[1:3] == ("--format=%s",)
            or args[1:4] == ("-1", "--pretty=%s")
            or (len(args) >= 2 and args[1] == "-1")
        ):
            return state["committed_subject"]
        if args[:1] == ("diff",):
            return ""
        if args[:2] == ("tag", "--list"):
            return "v0.0.0.96"
        if args[:2] == ("ls-remote", "--tags"):
            return ""
        if args[:1] == ("rev-parse",):
            return "deadbeef" * 5
        if args[0] == "show":
            return ""
        if args[0] == "add":
            return ""
        if args[0] == "commit":
            if len(args) >= 3 and args[1] == "-m":
                state["committed_subject"] = args[2]
            return ""
        if args[:2] == ("push", "origin"):
            return ""
        return ""

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: "")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **kwargs: type("R", (), {
                            "stdout": "10 passed in 1s\n",
                            "stderr": "",
                            "returncode": 0,
                        })())
    monkeypatch.setattr(release, "scan_staged_secrets", lambda _r: None)
    monkeypatch.setattr(release, "require_ukrainian", lambda *_a, **_kw: None)

    release.prepare_release(target_root, skip_tests=True)

    notes_path = target_root / "releases" / f"{version}.md"
    assert notes_path.exists()
    header = notes_path.read_text(encoding="utf-8").splitlines()[0]
    assert header == f"# Vertep {version}"
    changelog = (target_root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## ПРАВИЛЬНА НАЗВА: {version}" in changelog
    assert "## Unreleased" in changelog
    notes_section = changelog.split(f"## ПРАВИЛЬНА НАЗВА: {version}", 1)[1]
    assert "- Додано перевірку release notes." in notes_section
    assert (target_root / "VERSION").read_text(encoding="utf-8").strip() == version


def test_push_emits_only_version_subject_and_does_not_run_release_workflow(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    (target_root / "VERSION").write_text("0.0.0.10\n", encoding="utf-8")
    (target_root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Тестова зміна.\n",
        encoding="utf-8",
    )
    (target_root / "releases").mkdir()
    (target_root / "releases" / "0.0.0.10.md").write_text(
        "# Vertep 0.0.0.10\n\n- Тестова зміна.\n", encoding="utf-8",
    )
    git_calls = []
    state = {"subject": "0.0.0.10"}

    def fake_git(r, *args):
        git_calls.append(args)
        if args[:2] == ("tag", "--list"):
            return "v0.0.0.10"
        if args[:2] == ("ls-remote", "--tags"):
            return ""
        if args[:1] == ("rev-parse",):
            return "cafebabe" * 5
        if args[:1] == ("log",) and (
            args[1:3] == ("--format=%s",)
            or args[1:4] == ("-1", "--pretty=%s")
            or (len(args) >= 2 and args[1] == "-1")
        ):
            return state["subject"]
        if args[:1] == ("status",):
            return ""
        if args[:1] == ("diff",):
            return ""
        if args[0] in ("fetch", "add", "show"):
            return ""
        if args[0] == "commit":
            if len(args) >= 3 and args[1] == "-m":
                state["subject"] = args[2]
            return ""
        if args[:2] == ("push", "origin"):
            return ""
        return ""

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: "")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **kwargs: type("R", (), {
                            "stdout": "1 passed\n", "stderr": "", "returncode": 0,
                        })())
    monkeypatch.setattr(release, "scan_staged_secrets", lambda _r: None)
    monkeypatch.setattr(release, "require_ukrainian", lambda *_a, **_kw: None)

    result = release.prepare_release(target_root, skip_tests=True)

    assert result == "0.0.0.11"
    commit_call = next(args for args in git_calls if args and args[0] == "commit")
    assert commit_call == ("commit", "-m", "0.0.0.11")
    # Workflow must not be triggered by push.
    assert not any("workflow" in (a or "") for args in git_calls for a in args)


def test_release_runs_push_first_then_triggers_workflow(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    (target_root / "VERSION").write_text("0.0.0.20\n", encoding="utf-8")
    (target_root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- Тест.\n",
        encoding="utf-8",
    )
    (target_root / "releases").mkdir()
    (target_root / "releases" / "0.0.0.20.md").write_text(
        "# Vertep 0.0.0.20\n\n- Тест.\n", encoding="utf-8",
    )
    sequence = []
    state = {"subject": "0.0.0.20", "dirty": True}

    class FakeTrigger:
        def run(self, root, *, workflow, sha):
            sequence.append(("run", workflow, sha))
            return 4242

        def wait(self, root, *, run_id, timeout):
            sequence.append(("wait", run_id, timeout))
            return {"conclusion": "success"}

        def failed_jobs(self, root, *, run_id):
            return ""

        def verify(self, root, *, version, expected_sha):
            sequence.append(("verify", version, expected_sha))

    def fake_git(r, *args):
        if args[:2] == ("tag", "--list"):
            return "v0.0.0.20"
        if args[:2] == ("ls-remote", "--tags"):
            return ""
        if args[:1] == ("rev-parse",):
            return "abcdef" * 5
        if args[:1] == ("log",) and (
            args[1:3] == ("--format=%s",)
            or args[1:4] == ("-1", "--pretty=%s")
            or (len(args) >= 2 and args[1] == "-1")
        ):
            return state["subject"]
        if args[:1] == ("status",) and args[1:3] == ("--porcelain", "--untracked-files=no"):
            return "M README.md\n" if state["dirty"] else ""
        if args[:1] == ("diff",):
            return ""
        if args[0] in ("fetch", "add", "show"):
            return ""
        if args[0] == "commit":
            if len(args) >= 3 and args[1] == "-m":
                state["subject"] = args[2]
            state["dirty"] = False
            return ""
        if args[:2] == ("push", "origin"):
            return ""
        return ""

    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "run", lambda *_a, **_kw: "")
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **kwargs: type("R", (), {
                            "stdout": "ok\n", "stderr": "", "returncode": 0,
                        })())
    monkeypatch.setattr(release, "scan_staged_secrets", lambda _r: None)
    monkeypatch.setattr(release, "require_ukrainian", lambda *_a, **_kw: None)

    result = release.orchestrate_release(
        target_root, skip_tests=True, timeout=10, trigger=FakeTrigger()
    )

    assert result == "0.0.0.21"
    # Push must run before the workflow is triggered.
    assert sequence[0][0] == "run"
    assert sequence[1][0] == "wait"
    assert sequence[2] == ("verify", "0.0.0.21", "abcdef" * 5)


def test_release_does_not_create_commit_when_tree_is_clean(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.21"
    (target_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (target_root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Готово.\n",
        encoding="utf-8",
    )
    (target_root / "releases").mkdir()
    (target_root / "releases" / f"{version}.md").write_text(
        f"# Vertep {version}\n\n- Готово.\n", encoding="utf-8",
    )

    seen = {"commit": False}

    def fake_git(r, *args):
        if seen["commit"]:
            seen["commit"] = True
        if args[:2] == ("status",) and args[1:3] == ("--porcelain", "--untracked-files=no"):
            return ""
        if args[:1] == ("rev-parse",):
            return "1234567" * 5
        if args[:1] == ("log",) and (
            args[1:3] == ("--format=%s",)
            or args[1:4] == ("-1", "--pretty=%s")
            or (len(args) >= 2 and args[1] == "-1")
        ):
            return version
        if args[0] == "fetch":
            return ""
        if args[0] == "commit":
            raise AssertionError("commit must not run when tree is clean")
        return ""

    monkeypatch.setattr(release, "git", fake_git)

    sequence = []

    class FakeTrigger:
        def run(self, root, *, workflow, sha):
            sequence.append(("run", sha))
            return 99

        def wait(self, root, *, run_id, timeout):
            sequence.append(("wait", run_id))
            return {"conclusion": "success"}

        def failed_jobs(self, root, *, run_id):
            return ""

        def verify(self, root, *, version, expected_sha):
            sequence.append(("verify", version, expected_sha))

    release.orchestrate_release(
        target_root, skip_tests=True, timeout=10, trigger=FakeTrigger()
    )

    assert sequence == [
        ("run", "1234567" * 5),
        ("wait", 99),
        ("verify", version, "1234567" * 5),
    ]


def test_release_failed_workflow_reports_failed_jobs(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.22"
    (target_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (target_root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## ПРАВИЛЬНА НАЗВА: {version}\n- Готово.\n",
        encoding="utf-8",
    )
    (target_root / "releases").mkdir()
    (target_root / "releases" / f"{version}.md").write_text(
        f"# Vertep {version}\n\n- Готово.\n", encoding="utf-8",
    )

    def fake_git(r, *args):
        if args[:2] == ("status",) and args[1:3] == ("--porcelain", "--untracked-files=no"):
            return ""
        if args[:1] == ("rev-parse",):
            return "abcdef" * 5
        if args[:1] == ("log",) and (
            args[1:3] == ("--format=%s",)
            or args[1:4] == ("-1", "--pretty=%s")
            or (len(args) >= 2 and args[1] == "-1")
        ):
            return version
        if args[0] == "fetch":
            return ""
        return ""

    monkeypatch.setattr(release, "git", fake_git)

    class FailingTrigger:
        def run(self, root, *, workflow, sha):
            return 7

        def wait(self, root, *, run_id, timeout):
            return {"conclusion": "failure", "name": "Vertep Release"}

        def failed_jobs(self, root, *, run_id):
            return "  • job «prepare» — failure\n      - step «Build images: failed»"

        def verify(self, root, *, version, expected_sha):
            raise AssertionError("verify must not run after failed workflow")

    try:
        release.orchestrate_release(
            target_root, skip_tests=True, timeout=10, trigger=FailingTrigger()
        )
    except RuntimeError as error:
        message = str(error)
        assert "failure" in message
        assert "job «prepare»" in message
        assert "Build images" in message
    else:
        raise AssertionError("Failed workflow must raise RuntimeError")


def test_verify_release_requires_runtime_bundle_and_tag_points_to_commit(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.23"
    expected_sha = "feedface" * 5

    def fake_gh_run(root, args):
        if args[:2] == ("repo", "view"):
            return "fylypovych/vertep"
        return ""

    def fake_json(root, args):
        if args and args[0] == "api" and args[1].endswith(f"git/ref/tags/{version}"):
            return {"object": {"sha": expected_sha, "type": "commit"}}
        if args and args[0] == "release":
            return {
                "tagName": version,
                "targetCommitish": "main",
                "assets": [
                    {"name": f"vertep-runtime-{version}.tar.gz"},
                    {"name": f"manifest-{version}.json"},
                    {"name": f"update-manifest-{version}.json"},
                    {"name": f"SHA256SUMS-{version}"},
                ],
            }
        return {}

    monkeypatch.setattr(release, "gh_run", fake_gh_run)
    monkeypatch.setattr(release, "gh_run_json", fake_json)

    release.verify_release(target_root, version, expected_sha)


def test_verify_release_rejects_missing_runtime_bundle(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.24"
    expected_sha = "feedface" * 5

    def fake_gh_run(root, args):
        if args[:2] == ("repo", "view"):
            return "fylypovych/vertep"
        return ""

    def fake_json(root, args):
        if args and args[0] == "api" and args[1].endswith(f"git/ref/tags/{version}"):
            return {"object": {"sha": expected_sha, "type": "commit"}}
        if args and args[0] == "release":
            return {
                "tagName": version,
                "targetCommitish": "main",
                "assets": [
                    {"name": f"manifest-{version}.json"},
                ],
            }
        return {}

    monkeypatch.setattr(release, "gh_run", fake_gh_run)
    monkeypatch.setattr(release, "gh_run_json", fake_json)

    try:
        release.verify_release(target_root, version, expected_sha)
    except RuntimeError as error:
        assert "vertep-runtime" in str(error)
    else:
        raise AssertionError("Missing runtime bundle must fail verification")


def test_release_workflow_supports_dispatch_and_only_publishes_from_release_commit():
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert 'gh release create "$version"' in workflow
    assert "vertep-runtime-$version.tar.gz" in workflow
    assert 'git push origin "refs/tags/$version"' in workflow
    # Workflow must not commit or push to main.
    assert "git commit" not in workflow
    assert "HEAD:main" not in workflow


def test_verify_release_dereferences_annotated_tag(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.96"
    expected_sha = "fd9d2a53f6f0de12089ec337224f93d30da32602"
    tag_object_sha = "bee90e93669669cb9063bdb252f63ae18ddcab0c"

    def fake_gh_run(root, args):
        if args[:2] == ("repo", "view"):
            return "fylypovych/vertep"
        return ""

    def fake_json(root, args):
        if args and args[0] == "api":
            if args[1].endswith(f"git/ref/tags/{version}"):
                return {"object": {"sha": tag_object_sha, "type": "tag"}}
            if args[1].endswith(f"git/tags/{tag_object_sha}"):
                return {"object": {"sha": expected_sha, "type": "commit"}}
        if args and args[0] == "release":
            return {
                "tagName": version,
                "targetCommitish": "main",
                "assets": [
                    {"name": f"vertep-runtime-{version}.tar.gz"},
                    {"name": f"manifest-{version}.json"},
                    {"name": f"update-manifest-{version}.json"},
                    {"name": f"SHA256SUMS-{version}"},
                ],
            }
        return {}

    monkeypatch.setattr(release, "gh_run", fake_gh_run)
    monkeypatch.setattr(release, "gh_run_json", fake_json)
    release.verify_release(target_root, version, expected_sha)


def test_verify_release_rejects_mismatched_tag_commit(tmp_path, monkeypatch):
    release = load_release_module()
    target_root = tmp_path
    version = "0.0.0.96"
    expected_sha = "fd9d2a53f6f0de12089ec337224f93d30da32602"
    wrong_sha = "deadbeef" * 5

    monkeypatch.setattr(release, "gh_run", lambda *a, **kw: "fylypovych/vertep")
    monkeypatch.setattr(release, "gh_run_json", lambda *a, **kw: (
        {"object": {"sha": wrong_sha, "type": "commit"}}
        if a[1][0] == "api"
        else {
            "tagName": version,
            "targetCommitish": "main",
            "assets": [
                {"name": f"vertep-runtime-{version}.tar.gz"},
                {"name": f"manifest-{version}.json"},
                {"name": f"update-manifest-{version}.json"},
                {"name": f"SHA256SUMS-{version}"},
            ],
        }
    ))
    try:
        release.verify_release(target_root, version, expected_sha)
    except RuntimeError as error:
        assert "вказує на" in str(error)
    else:
        raise AssertionError("Mismatched tag commit must fail verification")
