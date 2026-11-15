"""Test helper: give a fixture character every §3.2 file (i.0.0.4.85).

A new job requires a complete character. Missing documents are written empty —
exactly what the loader read for an absent file before — and anything the fixture
writes afterwards overrides them.
"""

from pathlib import Path

from core.configuration import REQUIRED_CHARACTER_FILES


def complete_character(directory) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_CHARACTER_FILES:
        path = directory / name
        if not path.exists():
            path.write_text("" if name.endswith(".txt") else "{}", encoding="utf-8")
    return directory
