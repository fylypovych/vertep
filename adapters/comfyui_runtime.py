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
