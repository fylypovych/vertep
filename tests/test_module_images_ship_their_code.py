"""The application module images run what the modules import, and their health check runs.

Characters 0.0.4.58 imported a new ``knowledge_logic`` that its Dockerfile never copied (the
container exits at start); Brands and Monitoring checked health with ``wget``, which
``python:slim`` does not have (Docker reports the healthy module unhealthy).
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
COMPONENTS = json.loads((ROOT / "config/components.json").read_text(encoding="utf-8"))["components"]
IMAGES = sorted(name for name, data in COMPONENTS.items()
                if (data.get("image") or {}).get("context", "").startswith("modules/"))


def _copied(dockerfile: str) -> set[str]:
    names = set()
    for line in re.findall(r"^COPY (?:--\S+ )*(.+)$", dockerfile, re.M):
        names.update(item.rstrip("/") for item in line.split()[:-1])
    return names


@pytest.mark.parametrize("component", IMAGES)
def test_module_image_copies_every_local_module_it_imports(component):
    image = COMPONENTS[component]["image"]
    context = ROOT / image["context"]
    dockerfile = (ROOT / image["file"]).read_text(encoding="utf-8")
    copied = _copied(dockerfile)
    if "." in copied or "*.py" in copied:
        return  # the whole folder or every module is copied
    local = {path.stem for path in context.glob("*.py")}
    missing = set()
    for name in sorted(copied):
        if not name.endswith(".py") or not (context / name).is_file():
            continue
        source = (context / name).read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*from \.(\w+) import", source, re.M))
        imported |= set(re.findall(rf"^\s*from modules\.{re.escape(context.name)}\.(\w+) import", source, re.M))
        imported |= set(re.findall(r"^\s*(?:from|import) (\w+)", source, re.M)) & local
        missing |= {f"{module}.py" for module in imported if module in local and f"{module}.py" not in copied}
    assert not missing, f"{image['file']} does not copy {sorted(missing)}"


@pytest.mark.parametrize("component", IMAGES)
def test_module_health_check_uses_a_tool_the_image_has(component):
    dockerfile = (ROOT / COMPONENTS[component]["image"]["file"]).read_text(encoding="utf-8")
    check = re.search(r"HEALTHCHECK[^\n]*\\\n\s*CMD ([^\n]+)|HEALTHCHECK[^\n]*CMD ([^\n]+)", dockerfile)
    if not check:
        return
    command = check.group(1) or check.group(2)
    for tool in ("wget", "curl"):
        if re.search(rf"\b{tool}\b", command):
            assert re.search(rf"(apt-get install|apk add)[^\n]*\b{tool}\b", dockerfile), \
                f"health check uses {tool}, which the image does not install"



@pytest.mark.parametrize("component", IMAGES)
def test_module_health_reports_ready_for_the_worker_supervisor(component):
    """The Worker marks an application instance ready only on ``"ready": true`` from /health;
    Monitoring answered ``{"status": "ok"}`` and could never be activated."""
    context = ROOT / COMPONENTS[component]["image"]["context"]
    source = "".join(path.read_text(encoding="utf-8") for path in sorted(context.glob("*.py")))
    if '"/health"' not in source:
        return
    helper = re.search(r"def health_check\([^)]*\)[^:]*:(.*?)(?=\ndef |\nclass |\Z)", source, re.S)
    answers = [helper.group(1)] if helper else []
    answers += re.findall(r'== "/health":[^\n]*\n(?:[^\n]*\n){0,2}', source)
    assert answers and all('"ready":' in block or "health_check()" in block for block in answers), \
        f'{component}: /health does not answer "ready"'
