"""Standalone service images must ship every module their service imports.

The tts/publisher/backup images install their own pip set and copy only a few
source paths, so an import added to the service (even one inside a function)
crashes the container at runtime instead of failing a unit test.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Import names provided by a differently named or bundled distribution.
PROVIDED_BY = {"pydantic": "fastapi", "starlette": "fastapi"}


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _image(dockerfile: Path) -> tuple[set[str], set[str]]:
    text = dockerfile.read_text(encoding="utf-8")
    packages = set()
    for line in re.findall(r"pip install[^\n]*", text):
        for token in line.split()[2:]:
            name = re.split(r"[\[=<>!]", token.strip("'\""), maxsplit=1)[0]
            if name and not name.startswith("-"):
                packages.add(name.lower())
    copied = set()
    for line in re.findall(r"^COPY ([^\n]+)$", text, re.M):
        copied.update(line.split()[:-1])
    return packages, copied


def _shipped(module: str, copied: set[str]) -> Path | None:
    relative = Path(*module.split("."))
    for candidate in (relative.with_suffix(".py"), relative / "__init__.py"):
        if any(candidate.as_posix() == item or candidate.as_posix().startswith(item.rstrip("/") + "/")
               for item in copied):
            return ROOT / candidate
    return None


@pytest.mark.parametrize("image, service", [
    ("tts", "services.tts_service"),
    ("publisher", "services.publisher_service"),
    ("backup", "services.backup_service"),
])
def test_service_image_ships_every_imported_module(image, service):
    packages, copied = _image(ROOT / "docker" / image / "Dockerfile")
    missing, seen, pending = [], set(), [service]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        source = _shipped(module, copied)
        if source is None:
            missing.append(f"{module} (source not copied into the image)")
            continue
        for name in _imports(source):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names:
                continue
            if (ROOT / top).is_dir():
                pending.append(name)
            elif PROVIDED_BY.get(top, top).lower() not in packages:
                missing.append(f"{top} (not installed by pip, imported by {module})")
    assert not missing, f"docker/{image}/Dockerfile: " + ", ".join(sorted(set(missing)))
