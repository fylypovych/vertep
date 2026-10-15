"""Release identity baked into the built runtime (i.0.0.1.38).

The appliance image must be able to prove its own deployment identity
(full commit SHA, version, environment) even though a production container
has no ``.git`` directory.  The image build writes the verified release
identity to ``RELEASE_IDENTITY.json``; :func:`resolve_release_sha` and
:func:`resolve_release_version` read it first.

Resolution is fail-closed: an identity file that is present but carries a
missing/``unknown``/truncated/mismatched SHA is returned verbatim so the
acceptance gate (``can_close_issue``) rejects it — it is never silently
replaced by an ambient value.  Ambient ``GITHUB_SHA``/``git`` are only used
when no identity file exists (CI checkout / local development).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_IDENTITY_FILENAME = "RELEASE_IDENTITY.json"


def _identity_path() -> Path:
    override = os.getenv("VERTEP_RELEASE_IDENTITY_FILE", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / _IDENTITY_FILENAME


def load_release_identity() -> dict[str, str] | None:
    """Return the baked release identity, or ``None`` when absent/unreadable.

    A present-but-malformed document returns a dict with empty values so the
    caller keeps its authoritative (fail-closed) behaviour instead of falling
    back to ambient variables.
    """
    path = _identity_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"version": "", "commit_sha": "", "environment": ""}
    if not isinstance(data, dict):
        return {"version": "", "commit_sha": "", "environment": ""}
    return {
        "version": str(data.get("version", "") or "").strip(),
        "commit_sha": str(data.get("commit_sha", "") or "").strip(),
        "environment": str(data.get("environment", "") or "").strip(),
    }


def _git_head_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=os.getcwd(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def resolve_release_sha() -> str:
    """Resolve this deployment's commit SHA.

    Priority: baked release identity (authoritative when present) →
    ``GITHUB_SHA`` env → ``git rev-parse HEAD``.  Returns an empty string when
    no source yields a SHA; ``VERSION``/``unknown`` are never used as a commit
    identity.
    """
    identity = load_release_identity()
    if identity is not None:
        return identity["commit_sha"]
    github_sha = os.getenv("GITHUB_SHA", "").strip()
    if github_sha:
        return github_sha
    return _git_head_sha()


def resolve_release_version() -> str:
    """Resolve the installed version, preferring the baked release identity."""
    identity = load_release_identity()
    if identity is not None and identity["version"]:
        return identity["version"]
    from .version import application_version
    return application_version().strip()


def resolve_release_environment() -> str:
    """Resolve the baked release environment label (e.g. ``production``)."""
    identity = load_release_identity()
    if identity is not None:
        return identity["environment"]
    return os.getenv("VERTEP_ENVIRONMENT", "").strip()


def is_full_sha(value: str) -> bool:
    """Whether ``value`` is a full 40-character lowercase hex commit SHA."""
    return bool(_FULL_SHA_RE.fullmatch((value or "").strip()))
