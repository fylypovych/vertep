import argparse
import datetime as dt
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
        capture_output=capture
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
                r"(?m)^## (\d+\.\d+\.\d+\.\d+)(?:\s+-|$)",
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
    match = re.search(
        rf"(?ms)^## {re.escape(version)}(?:\s+-[^\n]*)?\s*\n(.*?)(?=^## |\Z)",
        changelog,
    )
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip().startswith("- ")]


def release_changelog(changelog: str, version: str, notes: list[str]) -> str:
    release = f"## {version} - {dt.date.today().isoformat()}\n\n" + "\n".join(notes) + "\n\n"
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
    if git(root, "status", "--porcelain"):
        raise RuntimeError("Для публікації релізу робоче дерево має бути чистим")
    subject = git(root, "log", "-1", "--pretty=%s")
    prefix = f"{version} — "
    if not subject.startswith(prefix):
        raise RuntimeError(f"Назва основного коміту має починатися з «{prefix}»")
    require_ukrainian(subject[len(prefix):], "Опис коміту")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = version_notes(changelog, version)
    if not notes:
        raise RuntimeError(f"У CHANGELOG.md немає опису версії {version}")
    require_ukrainian("\n".join(notes), "Опис змін у CHANGELOG.md")
    release_path = root / "releases" / f"{version}.md"
    if not release_path.exists():
        raise RuntimeError(f"Відсутній файл {release_path.relative_to(root)}")
    release_text = release_path.read_text(encoding="utf-8")
    if f"# Vertep {version}" not in release_text:
        raise RuntimeError("Файл нотаток релізу містить неправильну версію")
    require_ukrainian(release_text, "Нотатки релізу")
    return version


def prepare_release(root: Path, *, title: str, skip_tests: bool) -> str:
    require_ukrainian(title, "Назва коміту")
    git(root, "fetch", "--tags", "origin")
    version = next_version(known_versions(root))
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    notes = unreleased_notes(changelog)
    if not notes:
        raise RuntimeError("Додайте український опис змін до секції Unreleased у CHANGELOG.md")
    require_ukrainian("\n".join(notes), "Опис змін у CHANGELOG.md")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    changelog_path.write_text(release_changelog(changelog, version, notes), encoding="utf-8")
    release_dir = root / "releases"
    release_dir.mkdir(exist_ok=True)
    release_path = release_dir / f"{version}.md"
    validation = "Перевірку пропущено явним параметром `--skip-tests`."
    if not skip_tests:
        run(root, [sys.executable, "-m", "compileall", "-q", "core", "adapters",
                   "worker", "scripts", "installer", "tests"], capture=False)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root,
                                text=True, encoding="utf-8", capture_output=True,
                                check=True)
        print(result.stdout, end="")
        summary = next((line.strip() for line in reversed(result.stdout.splitlines())
                        if "passed" in line), "pytest успішно завершено")
        validation = f"`{summary}`"
    release_path.write_text(
        f"# Vertep {version}\n\nДата випуску: {dt.date.today().isoformat()}\n\n"
        "## Зміни\n\n" + "\n".join(notes)
        + "\n\n## Перевірка\n\n" + f"- {validation}\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    scan_staged_secrets(root)
    git(root, "commit", "-m", f"{version} — {title}")
    check_release(root)
    return version


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Підготувати єдиний нумерований коміт релізу Vertep"
    )
    parser.add_argument("--title", help="короткий український опис після номера версії")
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
        if not args.title:
            parser.error("для підготовки релізу потрібен параметр --title українською")
        version = prepare_release(root, title=args.title, skip_tests=args.skip_tests)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(f"Підготовлено коміт релізу {version}. Перегляньте його та надішліть у main.")


if __name__ == "__main__":
    main()
