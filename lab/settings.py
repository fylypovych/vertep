"""LAB configuration through the one LAB API and CLI (#491 L4).

The owner sets the GitHub repository, the Vertep GPU resource address, its CA,
the Vertep URL and GPU idle sharing in the panel or with ``lab settings``; both
go through :class:`LabSettings`, so the two surfaces cannot drift apart.
Secrets (GitHub token, resource token) are write-only: a read reports only that
they are set, and the audit trail records field names, never values.

The store is ``<state>/settings.json`` (0600) in the LAB state directory; the
runtime reads it over the environment (``apply_environment``), so an empty
stored value falls back to what ``lab.env`` provides.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .state_store import read_json, write_json

# field -> environment variable the runtime already reads
FIELDS = {
    "github_repository": "LAB_GITHUB_REPOSITORY",
    "resource_url": "LAB_RESOURCE_URL",
    "vertep_url": "LAB_VERTEP_URL",
    "idle_sharing_enabled": "LAB_IDLE_SHARING_ENABLED",
}
SECRETS = {"github_token": "LAB_GITHUB_TOKEN", "resource_token": "LAB_RESOURCE_TOKEN"}
CA_FIELD, CA_ENV = "resource_ca_pem", "LAB_RESOURCE_CA_FILE"
_ENVS = [*FIELDS.values(), *SECRETS.values(), CA_ENV]
_overlaid: dict = {}  # env -> value before the overlay


class SettingsError(ValueError):
    pass


def _url(value: str) -> str:
    if value and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", value):
        raise SettingsError("address must be an http(s) URL")
    return value.rstrip("/")


def _validate(field: str, value) -> str:
    if field == "idle_sharing_enabled":
        if not isinstance(value, bool):
            raise SettingsError("idle_sharing_enabled must be boolean")
        return "true" if value else "false"
    if not isinstance(value, str):
        raise SettingsError(f"{field} must be a string")
    value = value.strip()
    if field == "github_repository" and value and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise SettingsError("github_repository must be owner/name")
    if field in ("resource_url", "vertep_url"):
        return _url(value)
    if field == CA_FIELD and value:
        from cryptography import x509
        try:
            x509.load_pem_x509_certificates(value.encode())
        except ValueError as error:
            raise SettingsError("resource_ca_pem must be a PEM certificate") from error
        return value + "\n"
    if field in SECRETS and any(ch in value for ch in "\r\n"):
        raise SettingsError(f"{field} must be one line")
    return value


class LabSettings:
    def __init__(self, gate, state_dir=None):
        self.gate = gate
        self.state_dir = Path(state_dir or gate.state_dir)
        self.path = self.state_dir / "settings.json"
        self.ca_path = self.state_dir / "resource-ca.pem"

    def stored(self) -> dict:
        value = read_json(self.path, default={})
        return value if isinstance(value, dict) else {}

    def show(self) -> dict:
        """Effective values; secrets only as «set», never their value."""
        stored = self.stored()
        environment = _original_environment()
        result: dict = {"sources": {}}
        for field, env in FIELDS.items():
            value = stored.get(field) or environment.get(env) or ""
            result[field] = value.lower() in {"true", "1", "yes"} if field == "idle_sharing_enabled" else value
            result["sources"][field] = "settings" if stored.get(field) else "env" if environment.get(env) else "unset"
        for field, env in SECRETS.items():
            result[f"{field}_set"] = bool(stored.get(field) or environment.get(env))
            result["sources"][field] = "settings" if stored.get(field) else "env" if environment.get(env) else "unset"
        result[CA_FIELD] = stored.get(CA_FIELD, "")
        result["resource_ca_configured"] = bool(stored.get(CA_FIELD) or environment.get(CA_ENV))
        return result

    def update(self, values: dict, *, actor: str) -> dict:
        if not isinstance(values, dict) or not values:
            raise SettingsError("settings must be a non-empty object")
        unknown = sorted(set(values) - set(FIELDS) - set(SECRETS) - {CA_FIELD})
        if unknown:
            raise SettingsError(f"unknown setting(s): {', '.join(unknown)}")
        stored = self.stored()
        for field, value in values.items():
            normalised = _validate(field, value)
            if normalised:
                stored[field] = normalised
            else:
                stored.pop(field, None)  # empty -> fall back to the environment
        write_json(self.path, stored, mode=0o600)
        if stored.get(CA_FIELD):
            write_json_text(self.ca_path, stored[CA_FIELD])
        # Names only: secret values and the CA never enter the audit trail.
        self.gate.trail.append(actor=actor, action="lab_settings:update", subject=",".join(sorted(values)),
                               decision="UPDATED", rule="owner_settings",
                               reason="changed: " + ", ".join(sorted(values)))
        apply_environment(self.state_dir)
        return self.show()


def write_json_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _original_environment() -> dict:
    """The environment as ``lab.env`` gave it, without the stored overlay."""
    return {env: _overlaid.get(env, os.environ.get(env, "")) for env in _ENVS}


def apply_environment(state_dir) -> None:
    """Overlay stored settings on the environment the runtime reads; env is the fallback."""
    for env, original in _overlaid.items():  # undo the previous overlay first
        if original:
            os.environ[env] = original
        else:
            os.environ.pop(env, None)
    _overlaid.clear()
    stored = read_json(Path(state_dir) / "settings.json", default={})
    stored = stored if isinstance(stored, dict) else {}
    overlay = {env: stored.get(field) for field, env in {**FIELDS, **SECRETS}.items()}
    overlay[CA_ENV] = str(Path(state_dir) / "resource-ca.pem") if stored.get(CA_FIELD) else None
    for env, value in overlay.items():
        if value:
            _overlaid[env] = os.environ.get(env, "")
            os.environ[env] = value
    from .identity import reset_cache
    reset_cache()  # a changed resource address or CA must not keep a stale CORE key
