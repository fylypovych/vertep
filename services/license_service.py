"""Isolated license-manager boundary for the Vertep runtime."""

import hashlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException


app = FastAPI(title="Vertep License Manager", version="1")


def _license() -> tuple[str, str]:
    value = os.getenv("VERTEP_LICENSE_KEY", "").strip()
    key_file = os.getenv("VERTEP_LICENSE_KEY_FILE", "")
    if key_file:
        try:
            value = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise HTTPException(503, "License key file is unavailable") from error
    config_root = os.getenv("CONFIG_ROOT", "")
    encrypted_store = Path(config_root) / "secrets.enc.json" if config_root else None
    if not value and (os.getenv("DATABASE_URL") or (encrypted_store and encrypted_store.is_file())):
        try:
            # Read only: this container mounts config read-only, so CORE owns the migration.
            from core.first_run import _file_envelope, _open_envelope, _read_encrypted_secrets
            envelope = _file_envelope()
            stored = _read_encrypted_secrets() or (_open_envelope(envelope) if envelope else {})
            value = str(stored.get("license_key") or "").strip()
        except Exception as error:  # noqa: BLE001 - database errors must also answer 503
            raise HTTPException(503, "Encrypted license store is unavailable") from error
    if not value:
        return "COMMUNITY", ""
    if len(value) > 16_384 or any(character in value for character in "\r\n\0"):
        raise HTTPException(503, "License key is invalid")
    return "LICENSED", hashlib.sha256(value.encode()).hexdigest()[:16]


@app.get("/health")
def health() -> dict:
    state, fingerprint = _license()
    return {"status": "HEALTHY", "state": state, "fingerprint": fingerprint}


@app.get("/status")
def status() -> dict:
    return health()
