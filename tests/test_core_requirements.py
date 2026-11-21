"""Every third-party package CORE imports is declared in requirements.txt (0.0.4.30: the CORE
image lacked `packaging`, so the module platform failed and every module page returned 500)."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"adapters", "core", "lab", "scripts", "worker"}
# Imported names that come with a declared package or are optional (guarded imports).
PROVIDED = {"pydantic": "fastapi", "starlette": "fastapi", "torch": None}
DISTRIBUTION = {"yaml": "pyyaml"}


def _declared() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
        if match and not line.lstrip().startswith("#"):
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def test_every_third_party_import_of_core_is_a_declared_requirement():
    imported = set()
    for path in (ROOT / "core").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    declared = _declared()
    missing = sorted(name for name in imported - LOCAL - set(sys.stdlib_module_names) - set(PROVIDED)
                     if DISTRIBUTION.get(name, name).lower() not in declared)
    assert not missing, f"CORE imports packages missing from requirements.txt: {missing}"
