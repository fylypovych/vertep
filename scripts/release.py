import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\.(\d+)$")
UKRAINIAN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")
SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----"),
]
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".pem", ".p12", ".pfx"}

RELEASE_WORKFLOW = "Vertep Release"
REQUIRED_ARTIFACT_NAMES = (
    "vertep-runtime-{version}.tar.gz",
    "manifest-{version}.json",
    "update-manifest-{version}.json",
    "SHA256SUMS-{version}",
)
TERMINAL_RUN_STATUSES = {"completed"}


def run(root: Path, command: list[str], *, capture: bool = True) -> str:
    result = subprocess.run(
        command, cwd=root, check=True, text=True, encoding="utf-8",
        errors="replace", capture_output=capture
    )
    return result.stdout.strip() if capture else ""


def run_allow_failure(root: Path, command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=root, text=True, encoding="utf-8",
        errors="replace", capture_output=True
    )


def git(root: Path, *args: str) -> str:
    return run(root, ["git", *args])


def next_version(versions_source: list[str]) -> str:
    versions = []
    for value in versions_source:
        match = VERSION_RE.fullmatch(value.strip())
        if not match:
            continue
        parts = tuple(int(part) for part in match.groups())
        if any(part > 99 for part in parts[1:]):
            continue
        versions.append(parts)
    parts = list(max(versions, default=(0, 0, 0, 0)))
    parts[3] += 1
    for index in (3, 2, 1):
        if parts[index] == 100:
            parts[index] = 0
            parts[index - 1] += 1
    return ".".join(str(part) for part in parts)


def known_versions(root: Path) -> list[str]:
    """Return every release number recorded by repository metadata."""
    versions = git(root, "tag", "--list").splitlines()
    try:
        remote = git(root, "ls-remote", "--tags", "origin")
        versions.extend(
            ref.split("/")[-1]
            for ref in remote.splitlines()
            if ref
        )
    except subprocess.CalledProcessError:
        pass
    versions.extend(
        match.group(1)
        for subject in git(root, "log", "--format=%s").splitlines()
        if (match := re.match(r"^(v?\d+\.\d+\.\d+\.\d+)(?:\s|$)", subject.strip()))
    )
    version_path = root / "VERSION"
    if version_path.exists():
        versions.append(version_path.read_text(encoding="utf-8").strip())
    changelog_path = root / "CHANGELOG.md"
    if changelog_path.exists():
        versions.extend(
            re.findall(
                r"(?m)^## (?:ПРАВИЛЬНА НАЗВА: )?(\d+\.\d+\.\d+\.\d+)(?:\s+-|$)",
                changelog_path.read_text(encoding="utf-8"),
            )
        )
    release_dir = root / "releases"
    if release_dir.exists():
        versions.extend(path.stem for path in release_dir.glob("*.md"))
    return versions


def unreleased_notes(changelog: str) -> list[str]:
    match = re.search(r"(?ms)^## Unreleased\s*\n(.*?)(?=^## |\Z)", changelog)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip().startswith("- ")]


def version_notes(changelog: str, version: str) -> list[str]:
    pattern = rf"(?ms)^## (?:ПРАВИЛЬНА НАЗВА: )?{re.escape(version)}(?:\s+-)?\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, changelog)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip().startswith("- ")]


def release_changelog(changelog: str, version: str, notes: list[str]) -> str:
    release = f"## ПРАВИЛЬНА НАЗВА: {version}\n" + "\n".join(notes) + "\n\n"
    pattern = re.compile(r"(?ms)^## Unreleased\s*\n.*?(?=^## |\Z)")
    if pattern.search(changelog):
        return pattern.sub("## Unreleased\n\n" + release, changelog, count=1)
    heading = "# Changelog\n\n"
    body = changelog[len(heading):] if changelog.startswith(heading) else changelog
    return heading + "## Unreleased\n\n" + release + body.lstrip()


def changed_files(root: Path) -> list[tuple[str, str]]:
    rows = []
    for line in git(root, "diff", "--cached", "--name-status").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[-1]))
    return rows


def scan_staged_secrets(root: Path) -> None:
    problems = []
    for _, relative in changed_files(root):
        path = Path(relative)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"заборонене ім’я файла із секретом: {relative}")
            continue
        try:
            content = git(root, "show", f":{relative}")
        except subprocess.CalledProcessError:
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            problems.append(f"можливий секрет у файлі: {relative}")
    if problems:
        raise RuntimeError("Реліз заблоковано:\n" + "\n".join(problems))


def require_ukrainian(value: str, label: str) -> None:
    if not UKRAINIAN_RE.search(value):
        raise RuntimeError(f"{label} має бути українською мовою")


def has_uncommitted_changes(root: Path) -> bool:
    try:
        return bool(git(root, "status", "--porcelain", "--untracked-files=no"))
    except subprocess.CalledProcessError:
        return False


def current_release_commit(root: Path) -> tuple[str, str]:
    """Return (version, SHA) of the latest release-shaped commit on HEAD."""
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError("Файл VERSION містить некоректний номер версії")
    sha = git(root, "rev-parse", "HEAD")
    return version, sha


def check_release(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_RE.fullmatch(version):
        raise RuntimeError("Файл VERSION містить некоректний номер версії")
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Для публікації релізу відстежувані файли мають бути закомічені")
    subject = git(root, "log", "-1", "--pretty=%s")
    if subject.strip() != version:
        raise RuntimeError(f"Назва основного коміту має бути «{version}»")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = version_notes(changelog, version)
    if not notes:
        raise RuntimeError(f"У CHANGELOG.md немає опису версії {version}")
    require_ukrainian("\n".join(notes), "Опис змін у CHANGELOG.md")
    release_path = root / "releases" / f"{version}.md"
    if not release_path.exists():
        raise RuntimeError(f"Відсутній файл {release_path.relative_to(root)}")
    release_text = release_path.read_text(encoding="utf-8")
    release_lines = release_text.splitlines()
    if not release_lines or release_lines[0].strip() != f"# Vertep {version}":
        raise RuntimeError("Файл нотаток релізу містить неправильну версію")
    require_ukrainian(release_text, "Нотатки релізу")
    return version


def prepare_release(root: Path, *, skip_tests: bool) -> str:
    try:
        git(root, "fetch", "--tags", "origin")
    except subprocess.CalledProcessError:
        pass
    version = next_version(known_versions(root))
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    notes = unreleased_notes(changelog)
    if not notes:
        raise RuntimeError("Додайте український опис змін до секції Unreleased у CHANGELOG.md")
    require_ukrainian("\n".join(notes), "Опис змін у CHANGELOG.md")
    if not skip_tests:
        run(root, [sys.executable, "-m", "compileall", "-q", "core", "adapters",
                   "worker", "scripts", "installer", "tests"], capture=False)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--ignore=tests/test_browser_e2e.py"],
            cwd=root, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=True
        )
        print(result.stdout, end="")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    changelog_path.write_text(release_changelog(changelog, version, notes), encoding="utf-8")
    release_dir = root / "releases"
    release_dir.mkdir(exist_ok=True)
    release_path = release_dir / f"{version}.md"
    release_path.write_text(
        f"# Vertep {version}\n\n" + "\n".join(notes) + "\n",
        encoding="utf-8",
    )
    # Update tracked files and add only the generated release note. Unrelated
    # untracked workspace files must never leak into a release commit.
    git(root, "add", "-u")
    git(root, "add", str(release_path.relative_to(root)))
    scan_staged_secrets(root)
    git(root, "commit", "-m", version)
    check_release(root)
    local_sha = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    git(root, "fetch", "origin", "main")
    remote_sha = git(root, "rev-parse", "origin/main")
    if local_sha != remote_sha:
        raise RuntimeError(f"Commit {local_sha} не потрапив у origin/main (remote: {remote_sha})")
    print(f"Пуш виконано. Commit {local_sha} присутній у origin/main.")
    return version


def gh_run(root: Path, args: list[str]) -> str:
    return run(root, ["gh", *args])


def gh_run_json(root: Path, args: list[str]) -> object:
    result = run_allow_failure(root, ["gh", *args])
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Команда gh {' '.join(args)} завершилась з помилкою: {stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def trigger_release_workflow(root: Path, sha: str) -> int:
    """Trigger Vertep Release for the given commit. Returns the run id."""
    try:
        subprocess.run(
            ["gh", "--version"], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True
        )
    except FileNotFoundError:
        raise RuntimeError("Клієнт gh не знайдено. Встановіть GitHub CLI.") from None
    result = subprocess.run(
        ["gh", "workflow", "run", RELEASE_WORKFLOW + ".yml", "--ref", sha],
        cwd=root, text=True, encoding="utf-8", errors="replace",
        capture_output=True
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "Не вдалося запустити GitHub Actions workflow "
            f"«{RELEASE_WORKFLOW}»: {stderr}"
        )
    # Discover the new run id (poll briefly because `gh workflow run` does not
    # return the id directly).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        runs = gh_run_json(root, [
            "run", "list", "--workflow", RELEASE_WORKFLOW + ".yml",
            "--commit", sha, "--json", "databaseId,status,conclusion,headSha",
            "--limit", "5"
        ])
        if isinstance(runs, list) and runs:
            for entry in runs:
                if entry.get("headSha", "").lower() == sha.lower():
                    return int(entry["databaseId"])
        time.sleep(2)
    raise RuntimeError(
        "GitHub Actions workflow запущено, але run id не вдалося визначити"
    )


def wait_for_workflow(root: Path, run_id: int, *, timeout: int) -> dict:
    """Wait until the workflow reaches a terminal status. Returns the run object."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = gh_run_json(root, [
            "run", "view", str(run_id),
            "--json", "databaseId,status,conclusion,name,headSha,url,displayTitle"
        ])
        status = run.get("status")
        if status in TERMINAL_RUN_STATUSES or run.get("conclusion"):
            return run
        time.sleep(10)
    raise RuntimeError(
        f"Очікування GitHub Actions workflow перевищило {timeout} секунд "
        f"(run id {run_id})."
    )


def _format_failed_jobs(root: Path, run_id: int) -> str:
    jobs = gh_run_json(root, ["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs",
                              "--jq", ".jobs[] | {name, conclusion, steps: [.steps[] | select(.conclusion==\"failure\") | {name, conclusion}]}"])
    if not isinstance(jobs, list) or not jobs:
        return "Деталі jobів недоступні."
    lines = []
    for job in jobs:
        name = job.get("name", "?")
        conclusion = job.get("conclusion") or "?"
        if conclusion != "failure":
            continue
        lines.append(f"  • job «{name}» — {conclusion}")
        for step in job.get("steps", []):
            lines.append(
                f"      - step «{step.get('name', '?')}»: {step.get('conclusion', '?')}"
            )
    return "\n".join(lines) if lines else "Без деталей jobів."


def _resolve_tag_commit_sha(root: Path, repo: str, version: str) -> str:
    """Return the commit SHA the given tag points to (dereferencing annotated tags)."""
    try:
        ref_payload = gh_run_json(root, ["api", f"repos/{repo}/git/ref/tags/{version}"])
    except RuntimeError as error:
        raise RuntimeError(
            f"Git tag «{version}» не створено. Перевірте лог Vertep Release."
        ) from error
    object_meta = ref_payload.get("object", {})
    object_type = object_meta.get("type", "commit")
    object_sha = (object_meta.get("sha") or "").lower()
    if not object_sha:
        raise RuntimeError(f"Git tag «{version}» не вказує на SHA.")
    if object_type == "tag":
        try:
            tag_payload = gh_run_json(root, [
                "api", f"repos/{repo}/git/tags/{object_sha}"
            ])
        except RuntimeError as error:
            raise RuntimeError(
                f"Annotated tag «{version}» пошкоджено: {error}"
            ) from error
        object_sha = (tag_payload.get("object", {}).get("sha") or "").lower()
        if not object_sha:
            raise RuntimeError(f"Annotated tag «{version}» не вказує на коміт.")
    return object_sha


def verify_release(root: Path, version: str, expected_sha: str) -> None:
    """Verify that tag, GitHub Release and required artifacts exist for `version`."""
    repo = gh_run(root, ["repo", "view", "--json", "nameWithOwner"])
    tag_commit_sha = _resolve_tag_commit_sha(root, repo, version)
    if tag_commit_sha != expected_sha.lower():
        raise RuntimeError(
            f"Git tag «{version}» вказує на {tag_commit_sha}, але очікувався {expected_sha}."
        )
    try:
        release_json = gh_run_json(root, [
            "release", "view", version,
            "--json", "tagName,name,targetCommitish,isDraft,isPrerelease,assets"
        ])
    except RuntimeError as error:
        raise RuntimeError(
            f"GitHub Release «{version}» не знайдено: {error}"
        ) from error
    asset_names = {asset.get("name") for asset in release_json.get("assets", [])}
    missing = [
        template.format(version=version)
        for template in REQUIRED_ARTIFACT_NAMES
        if template.format(version=version) not in asset_names
    ]
    if missing:
        raise RuntimeError(
            "GitHub Release «{v}» не містить обов'язкових артефактів: {missing}".format(
                v=version, missing=", ".join(missing)
            )
        )


def orchestrate_release(
    root: Path,
    *,
    skip_tests: bool,
    timeout: int,
    trigger: "_ProductionTrigger | None" = None,
) -> str:
    """End-to-end `реліз`: optional push, trigger workflow, wait, verify."""
    if trigger is None:
        trigger = _ProductionTrigger()
    if has_uncommitted_changes(root):
        version = prepare_release(root, skip_tests=skip_tests)
    else:
        version, _sha = current_release_commit(root)
        check_release(root)
    git(root, "fetch", "origin", "main")
    remote_sha = git(root, "rev-parse", "origin/main")
    local_sha = git(root, "rev-parse", "HEAD")
    if local_sha != remote_sha:
        raise RuntimeError(
            f"Локальний HEAD ({local_sha}) відрізняється від origin/main ({remote_sha})."
        )
    run_id = trigger.run(root, workflow=RELEASE_WORKFLOW, sha=local_sha)
    run = trigger.wait(root, run_id=run_id, timeout=timeout)
    conclusion = run.get("conclusion")
    if conclusion != "success":
        failed_jobs = trigger.failed_jobs(root, run_id=run_id)
        raise RuntimeError(
            f"GitHub Actions workflow «{RELEASE_WORKFLOW}» завершився зі статусом "
            f"«{conclusion}».\n{failed_jobs}"
        )
    trigger.verify(root, version=version, expected_sha=local_sha)
    print(
        f"Release {version} створено успішно (workflow run {run_id}). "
        f"Commit {local_sha} присутній у origin/main."
    )
    return version


class _ProductionTrigger:
    """Default implementation that shells out to the GitHub CLI."""

    def run(self, root: Path, *, workflow: str, sha: str) -> int:
        return trigger_release_workflow(root, sha)

    def wait(self, root: Path, *, run_id: int, timeout: int) -> dict:
        return wait_for_workflow(root, run_id, timeout=timeout)

    def failed_jobs(self, root: Path, *, run_id: int) -> str:
        return _format_failed_jobs(root, run_id)

    def verify(self, root: Path, *, version: str, expected_sha: str) -> None:
        verify_release(root, version, expected_sha)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Підготувати єдиний нумерований коміт релізу Vertep"
    )
    parser.add_argument("--skip-tests", action="store_true",
                        help="пропустити перевірку (не рекомендовано)")
    parser.add_argument("--show-next", action="store_true",
                        help="показати наступний номер без змін")
    parser.add_argument("--check", action="store_true",
                        help="перевірити готовий релізний коміт")
    parser.add_argument("--release", action="store_true",
                        help="запустити повну команду «реліз»: пуш + Vertep Release + перевірка")
    parser.add_argument("--release-timeout", type=int, default=3600,
                        help="максимальний час очікування GitHub Actions (секунди)")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.show_next:
            print(next_version(known_versions(root)))
            return
        if args.check:
            print(check_release(root))
            return
        if args.release:
            orchestrate_release(
                root,
                skip_tests=args.skip_tests,
                timeout=args.release_timeout,
            )
            return
        version = prepare_release(root, skip_tests=args.skip_tests)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(f"Підготовлено коміт релізу {version} та відправлено його у main.")


if __name__ == "__main__":
    main()