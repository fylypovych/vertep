import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path


VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\.(\d+)$")
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
    result = subprocess.run(command, cwd=root, check=True, text=True,
                            capture_output=capture)
    return result.stdout.strip() if capture else ""


def git(root: Path, *args: str) -> str:
    return run(root, ["git", *args])


def next_version(tags: list[str]) -> str:
    versions = []
    for tag in tags:
        match = VERSION_RE.fullmatch(tag.strip())
        if not match:
            continue
        parts = tuple(int(value) for value in match.groups())
        if any(value > 99 for value in parts[1:]):
            continue
        versions.append(parts)
    parts = list(max(versions, default=(0, 0, 0, 0)))
    parts[3] += 1
    for index in (3, 2, 1):
        if parts[index] == 100:
            parts[index] = 0
            parts[index - 1] += 1
    return ".".join(str(value) for value in parts)


def known_versions(root: Path) -> list[str]:
    """Return every release number recorded by repository metadata.

    Older releases were merged without tags, so tags alone are not a reliable
    source for the next sequence number.
    """
    versions = git(root, "tag", "--list").splitlines()
    versions.extend(subject for subject in git(root, "log", "--format=%s").splitlines()
                    if VERSION_RE.fullmatch(subject.strip()))
    version_path = root / "VERSION"
    if version_path.exists():
        versions.append(version_path.read_text(encoding="utf-8").strip())
    changelog_path = root / "CHANGELOG.md"
    if changelog_path.exists():
        versions.extend(re.findall(
            r"(?m)^## (\d+\.\d+\.\d+\.\d+)(?:\s+-|$)",
            changelog_path.read_text(encoding="utf-8"),
        ))
    release_dir = root / "releases"
    if release_dir.exists():
        versions.extend(path.stem for path in release_dir.glob("*.md"))
    return versions


def unreleased_notes(changelog: str) -> list[str]:
    match = re.search(r"(?ms)^## Unreleased\s*\n(.*?)(?=^## |\Z)", changelog)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.strip().startswith("- ")]


def changed_files(root: Path) -> list[tuple[str, str]]:
    rows = []
    for line in git(root, "diff", "--cached", "--name-status").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0], parts[-1]))
    return rows


def generated_notes(root: Path, files: list[tuple[str, str]]) -> list[str]:
    categories = {
        "CORE, API та оркестрація": ("core/", "agents/"),
        "Worker і медіаадаптери": ("worker/", "adapters/", "workflows/", "publishers/", "telegram/"),
        "Вебінтерфейс": ("web/",),
        "Встановлення й експлуатація": ("installer/", "scripts/", "docker-compose", "Dockerfile", "install.sh"),
        "Тести й безперервна інтеграція": ("tests/", ".github/"),
        "Конфігурація та документація": ("config/", "characters/", "brands/", "prompts/", "README", "CHANGELOG", "TZ_", ".env.example"),
    }
    notes = []
    for title, prefixes in categories.items():
        count = sum(any(path.startswith(prefix) for prefix in prefixes) for _, path in files)
        if count:
            notes.append(f"- {title}: змінено файлів — {count}.")
    if not notes:
        notes.append(f"- Обслуговування репозиторію: змінено файлів — {len(files)}.")
    release_commits = git(root, "log", "--format=%H%x09%s").splitlines()
    baseline = next((row.split("\t", 1)[0] for row in release_commits
                     if len(row.split("\t", 1)) == 2
                     and (VERSION_RE.fullmatch(row.split("\t", 1)[1])
                          or re.fullmatch(r"Release \d+\.\d+\.\d+\.\d+", row.split("\t", 1)[1]))), "")
    revision_range = f"{baseline}..HEAD" if baseline else "HEAD"
    subjects = [line for line in git(root, "log", "--format=%s", "--no-merges", revision_range).splitlines()
                if line and not re.fullmatch(r"Release \d+\.\d+\.\d+\.\d+", line)
                and not VERSION_RE.fullmatch(line)]
    notes.extend(f"- Коміт: {subject}" for subject in subjects[:20])
    return notes


def release_changelog(changelog: str, version: str, notes: list[str]) -> str:
    release = f"## {version} - {dt.date.today().isoformat()}\n\n" + "\n".join(notes) + "\n\n"
    pattern = re.compile(r"(?ms)^## Unreleased\s*\n.*?(?=^## |\Z)")
    if pattern.search(changelog):
        return pattern.sub("## Unreleased\n\n" + release, changelog, count=1)
    heading = "# Changelog\n\n"
    body = changelog[len(heading):] if changelog.startswith(heading) else changelog
    return heading + "## Unreleased\n\n" + release + body.lstrip()


def scan_staged_secrets(root: Path) -> None:
    problems = []
    for _, relative in changed_files(root):
        path = Path(relative)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"forbidden secret filename: {relative}")
            continue
        try:
            content = git(root, "show", f":{relative}")
        except subprocess.CalledProcessError:
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            problems.append(f"possible secret in: {relative}")
    if problems:
        raise RuntimeError("Release blocked:\n" + "\n".join(problems))


def prepare_release(root: Path, *, push: bool, skip_tests: bool) -> str:
    if git(root, "branch", "--show-current") == "":
        raise RuntimeError("Cannot release from a detached HEAD")
    remote = git(root, "remote", "get-url", "origin")
    if "github.com" not in remote:
        raise RuntimeError("origin is not a GitHub repository")
    git(root, "fetch", "--tags", "origin")
    version = next_version(known_versions(root))
    git(root, "add", "-A")
    files = changed_files(root)
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else "# Changelog\n\n## Unreleased\n"
    notes = unreleased_notes(changelog) or generated_notes(root, files)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    changelog_path.write_text(release_changelog(changelog, version, notes), encoding="utf-8")
    release_dir = root / "releases"
    release_dir.mkdir(exist_ok=True)
    release_path = release_dir / f"{version}.md"
    release_path.write_text(f"# Vertep {version}\n\nДата випуску: {dt.date.today().isoformat()}\n\n## Зміни\n\n"
                            + "\n".join(notes) + "\n\n## Перевірка\n\n- Очікується.\n", encoding="utf-8")
    git(root, "add", "-A")
    scan_staged_secrets(root)
    validation = "Пропущено явним параметром --skip-tests."
    if not skip_tests:
        run(root, [sys.executable, "-m", "compileall", "-q", "core", "adapters", "worker", "scripts", "installer", "tests"], capture=False)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root, text=True,
                                capture_output=True, check=True)
        print(result.stdout, end="")
        validation = next((line.strip() for line in reversed(result.stdout.splitlines()) if "passed" in line), "pytest passed")
    release_text = release_path.read_text(encoding="utf-8").replace("- Очікується.", f"- `{validation}`")
    release_path.write_text(release_text, encoding="utf-8")
    git(root, "add", "-A")
    scan_staged_secrets(root)
    git(root, "commit", "-m", version)
    tag = version
    git(root, "tag", "-a", tag, "-m", f"Vertep {version}\n\n" + "\n".join(notes))
    if push:
        branch = git(root, "branch", "--show-current")
        run(root, ["git", "push", "--atomic", "origin", f"HEAD:{branch}", tag], capture=False)
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the next sequential Vertep release and push it to GitHub")
    parser.add_argument("--no-push", action="store_true", help="Create the commit and tag locally without pushing")
    parser.add_argument("--skip-tests", action="store_true", help="Skip validation (not recommended)")
    parser.add_argument("--show-next", action="store_true", help="Print the next version without changing files")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.show_next:
        print(next_version(known_versions(root)))
        return
    try:
        version = prepare_release(root, push=not args.no_push, skip_tests=args.skip_tests)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(f"Released {version}{' locally' if args.no_push else ' to GitHub'}")


if __name__ == "__main__":
    main()
