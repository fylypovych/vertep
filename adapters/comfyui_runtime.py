"""Pinned ComfyUI runtime inventory and its readiness/checksum gates.

Issue #83 requires that the GPU runtime is described by an *inventory* rather
than by a handful of build arguments, and that every negative path (missing
inventory, version drift, missing or tampered workflow/model) fails closed
instead of silently falling back to whatever happens to be on disk.

The inventory is a JSON contract:

```json
{
  "format": "comfyui_runtime/v1",
  "components": {
    "comfyui": {"version": "v0.34.0", "commit": "<40 hex>"},
    "ComfyUI-VideoHelperSuite": {"version": "1.0.4", "commit": "<40 hex>"}
  },
  "workflows": {"image/demo.json": {"sha256": "<64 hex>"}},
  "models": {"model.safetensors": {"sha256": "<64 hex>"}}
}
```

``components`` records what the image was actually built from (the Dockerfile
resolves the real git commits at build time), ``workflows``/``models`` pin the
content the GPU node executes.  Verification is opt-in through
``COMFYUI_RUNTIME_INVENTORY``; when the variable is unset the node keeps its
current behaviour so a bare development checkout still works.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

INVENTORY_FORMAT = "comfyui_runtime/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: The immutable build pins of the ComfyUI image and the Dockerfile arguments
#: that carry them.  The argument is the single source of truth: the expected
#: inventory and the build report are both derived from it, so a pin can never
#: be recorded in one place and built from another (Issue #120/#178.1).
PINNED_COMPONENTS: dict[str, str] = {
    "COMFYUI_COMMIT": "comfyui",
    "VHS_COMMIT": "ComfyUI-VideoHelperSuite",
}


class RuntimeInventoryError(RuntimeError):
    """Raised when the GPU runtime does not match its pinned inventory."""


def inventory_path() -> Path | None:
    configured = os.getenv("COMFYUI_RUNTIME_INVENTORY", "").strip()
    return Path(configured) if configured else None


def load_inventory(path: Path | None = None) -> dict:
    """Load and structurally validate the pinned inventory."""
    target = path or inventory_path()
    if target is None:
        raise RuntimeInventoryError("COMFYUI_RUNTIME_INVENTORY is not configured")
    if not target.is_file():
        raise RuntimeInventoryError(f"Pinned runtime inventory is missing: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeInventoryError(f"Pinned runtime inventory is unreadable: {error}") from error
    if not isinstance(data, dict) or data.get("format") != INVENTORY_FORMAT:
        raise RuntimeInventoryError(
            f"Pinned runtime inventory must use format {INVENTORY_FORMAT!r}")
    if not isinstance(data.get("components"), dict) or not data["components"]:
        raise RuntimeInventoryError("Pinned runtime inventory declares no components")
    return data


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_components(inventory: dict, observed: dict) -> list[str]:
    """Compare installed component versions/commits with the pinned inventory.

    ``observed`` maps a component name to its installed ``version``/``commit``.
    Returns the list of drifted components; raises when an entry is malformed.
    """
    problems: list[str] = []
    for name, expected in inventory["components"].items():
        if not isinstance(expected, dict):
            raise RuntimeInventoryError(f"Component {name!r} has no pinned record")
        commit = expected.get("commit")
        if commit is not None and not _COMMIT.match(str(commit)):
            raise RuntimeInventoryError(f"Component {name!r} has a malformed pinned commit")
        actual = observed.get(name) or {}
        if expected.get("version") and actual.get("version") != expected["version"]:
            problems.append(f"{name}: pinned {expected['version']}, installed {actual.get('version') or 'unknown'}")
        if commit and actual.get("commit") != commit:
            problems.append(f"{name}: pinned commit {commit[:12]}, installed "
                            f"{str(actual.get('commit') or 'unknown')[:12]}")
    return problems


def verify_file_set(entries: dict, base: Path, label: str) -> list[str]:
    """Check that every pinned file exists and still hashes to its pinned value."""
    problems: list[str] = []
    for relative, expected in (entries or {}).items():
        if not isinstance(expected, dict):
            raise RuntimeInventoryError(f"Pinned {label} entry {relative!r} is malformed")
        digest = expected.get("sha256")
        if not digest or not _SHA256.match(str(digest)):
            raise RuntimeInventoryError(f"Pinned {label} entry {relative!r} has no valid sha256")
        candidate = (base / relative).resolve()
        if base.resolve() not in candidate.parents and candidate != base.resolve():
            raise RuntimeInventoryError(f"Pinned {label} entry {relative!r} escapes its root")
        if not candidate.is_file():
            problems.append(f"{label} {relative}: missing")
            continue
        actual = file_sha256(candidate)
        if actual != digest:
            problems.append(f"{label} {relative}: checksum mismatch")
    return problems


def verify_runtime(observed: dict | None = None, *, workflows_root: Path | None = None,
                   models_root: Path | None = None, path: Path | None = None) -> dict:
    """Verify the whole pinned contract and return the observed inventory.

    ``observed`` describes the *installed* components.  When it is omitted the
    inventory is verified only for content (workflows/models), which is what a
    node that does not run ComfyUI itself can check.
    """
    inventory = load_inventory(path)
    problems: list[str] = []
    if observed is not None:
        problems.extend(verify_components(inventory, observed))
    workflow_base = workflows_root or Path(os.getenv("WORKFLOWS_ROOT", "workflows"))
    problems.extend(verify_file_set(inventory.get("workflows", {}), workflow_base, "workflow"))
    model_base = models_root or Path(os.getenv("MODELS_ROOT", "/data/models"))
    problems.extend(verify_file_set(inventory.get("models", {}), model_base, "model"))
    if problems:
        raise RuntimeInventoryError("Pinned GPU runtime does not match: " + "; ".join(problems))
    return {
        "format": inventory["format"],
        "components": inventory["components"],
        "workflows": sorted(inventory.get("workflows", {})),
        "models": sorted(inventory.get("models", {})),
    }


def installed_components(comfyui_root: Path | None = None) -> dict:
    """Read the component records the image writes at build time."""
    root = comfyui_root or Path(os.getenv("COMFYUI_ROOT", "/opt/comfyui"))
    report = root / "runtime-inventory.json"
    if not report.is_file():
        return {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("components", {}) if isinstance(data, dict) else {}


def pinned_components(dockerfile: str) -> dict:
    """Read the immutable build pins out of the ComfyUI Dockerfile.

    A pin is a full commit: a floating branch or tag is refused, because the
    build input has to be the immutable identity the node later verifies.
    """
    components: dict[str, dict] = {}
    for argument, name in PINNED_COMPONENTS.items():
        match = re.search(rf"^ARG {argument}=([0-9a-f]{{40}})$", dockerfile, re.MULTILINE)
        if not match:
            raise RuntimeInventoryError(
                f"Pinned build argument {argument} is missing or is not an immutable commit")
        commit = match.group(1)
        components[name] = {"version": commit, "commit": commit}
    return components


def digest_tree(root: Path | None) -> dict:
    """Digest every file under ``root`` as ``{relative path: {sha256}}``.

    The relative path is POSIX-style, so the inventory is portable between the
    machine that produced it and the node that verifies it.
    """
    if root is None or not root.is_dir():
        return {}
    entries: dict[str, dict] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        entries[relative] = {"sha256": file_sha256(path)}
    return entries


def pinned_document(dockerfile: str) -> dict:
    """The pins-only document shipped with the runtime bundle.

    The bundle is installed on a host that has no source checkout and therefore
    no Dockerfile; the immutable component pins travel with it so the
    deployment can still write the expected inventory of the GPU node.
    """
    return {"format": INVENTORY_FORMAT, "components": pinned_components(dockerfile),
            "workflows": {}, "models": {}}

def build_expected_inventory(*, components: dict, workflows_root: Path | None = None,
                             models_root: Path | None = None) -> dict:
    """Build the *expected* runtime inventory of the GPU node.

    The expected inventory is the contract the node verifies itself against: the
    immutable component pins of the image, plus the content of the workflows and
    models the node is supposed to serve.  It is produced from the pins of the
    Dockerfile that builds the image, so the pin and the recorded commit can
    never diverge (Issue #120/#178.1).
    """
    if not components:
        raise RuntimeInventoryError("Pinned runtime inventory declares no components")
    return {
        "format": INVENTORY_FORMAT,
        "components": components,
        "workflows": digest_tree(workflows_root),
        "models": digest_tree(models_root),
    }


def expected_inventory_from_dockerfile(dockerfile: str, *, workflows_root: Path | None = None,
                                       models_root: Path | None = None) -> dict:
    """Build the expected inventory from a ComfyUI Dockerfile."""
    return build_expected_inventory(components=pinned_components(dockerfile),
                                    workflows_root=workflows_root,
                                    models_root=models_root)


def write_expected_inventory(path: Path, inventory: dict) -> None:
    """Write the expected inventory to the path the Worker reads it from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
