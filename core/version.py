import os
from pathlib import Path


def installation_root() -> Path:
    """Root of the Vertep installation that owns the running runtime."""
    override = os.getenv("VERTEP_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1]


def application_version() -> str:
    """Version of the active runtime instance.

    A signed update materialises an immutable release below ``releases/`` and
    switches the ``current`` pointer to it, so the version of the runtime that
    is actually active is read from ``current/VERSION``.  The mutable copy in
    the installation root stays a fallback for installations without a release
    layout.
    """
    root = installation_root()
    for candidate in (root / "current" / "VERSION", root / "VERSION"):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "0.0.0.0"
