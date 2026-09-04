import argparse
import re
import subprocess
import sys
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


def run(root: Path, command: list[str], *, capture: bool = True) -> str:
    result = subprocess.run(
        command, cwd=root, check=True, text=True, encoding="utf-8",
        errors="replace", capture_output=capture
    )
    return result.stdout.strip() if capture else ""


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
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.show_next:
            print(next_version(known_versions(root)))
            return
        if args.check:
            print(check_release(root))
            return
        version = prepare_release(root, skip_tests=args.skip_tests)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(f"Підготовлено коміт релізу {version} та відправлено його у main.")


if __name__ == "__main__":
    main()
