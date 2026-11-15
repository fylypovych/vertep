"""Every CHANGELOG version has its release notes file with the exact header (§2.5, §2.8).

Notes 0.0.2.91, 0.0.3.7 and 0.0.3.65 were restored later (audit-1511 A9/В2); the
restored files say so explicitly instead of passing for part of the historical commit.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESTORED = {"0.0.2.91": "e3c1f705", "0.0.3.7": "a58567d8", "0.0.3.65": "aeb4533a"}


def _versions() -> list[str]:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## ПРАВИЛЬНА НАЗВА: (\d+\.\d+\.\d+\.\d+)$", text, flags=re.M)


def test_every_changelog_version_has_notes_with_the_exact_header():
    missing, wrong = [], []
    for version in _versions():
        path = ROOT / "releases" / f"{version}.md"
        if not path.is_file():
            missing.append(version)
        elif path.read_text(encoding="utf-8").splitlines()[0] != f"# Vertep {version}":
            wrong.append(version)
    assert missing == [] and wrong == [], (missing, wrong)


def test_restored_notes_state_the_late_restoration():
    for version, commit in RESTORED.items():
        text = (ROOT / "releases" / f"{version}.md").read_text(encoding="utf-8")
        assert "відновлено пізніше" in text and commit in text, version
        assert "історію, SHA і tags не змінено" in text, version
