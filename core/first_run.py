"""Persistent bootstrap state and first-run setup for appliance installations."""

import base64
import hashlib
import json
import os
import platform
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


_secret_lock = threading.RLock()
_SECRET_AAD = b"vertep-secret-store:v1"
_KEY_AAD = b"vertep-secret-store-key:v1"
INTEGRATION_SECRET_NAMES = frozenset({
    "smtp_password", "telegram_bot_token", "youtube_client_secret",
    "facebook_access_token", "tiktok_client_secret", "external_ai_api_key",
    "license_key", "ssh_private_key",
})


def config_root() -> Path:
    return Path(os.getenv("CONFIG_ROOT", "/data/config"))


def _read(name: str, default: dict | None = None) -> dict:
    try:
        value = json.loads((config_root() / name).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def _write(name: str, value: dict) -> None:
    root = config_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    temporary = root / f".{name}.tmp"
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _key_encryption_key(salt: bytes) -> bytes | None:
    passphrase = os.getenv("SECRET_STORE_PASSPHRASE", "")
    if not passphrase:
        if os.getenv("REQUIRE_SECRET_KEY_SEALING", "false").lower() == "true":
            raise RuntimeError("Secret-store key sealing is required but no passphrase is configured")
        return None
    if len(passphrase) < 16:
        raise ValueError("SECRET_STORE_PASSPHRASE must contain at least 16 characters")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def _write_sealed_key(path: Path, key: bytes) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    kek = _key_encryption_key(salt)
    if kek is None:
        path.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="ascii")
    else:
        path.write_text(json.dumps({"version": 1, "algorithm": "scrypt+A256GCM",
                                    "salt": base64.b64encode(salt).decode(),
                                    "nonce": base64.b64encode(nonce).decode(),
                                    "ciphertext": base64.b64encode(
                                        AESGCM(kek).encrypt(nonce, key, _KEY_AAD)).decode()}),
                        encoding="utf-8")
    os.chmod(path, 0o600)


def _decode_key(encoded: str) -> bytes:
    if encoded.lstrip().startswith("{"):
        try:
            envelope = json.loads(encoded)
            if envelope.get("version") != 1 or envelope.get("algorithm") != "scrypt+A256GCM":
                raise ValueError("unsupported secret-store key envelope")
            salt = base64.b64decode(envelope["salt"], validate=True)
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            kek = _key_encryption_key(salt)
            if kek is None:
                raise RuntimeError("A passphrase is required to unseal the secret-store key")
            return AESGCM(kek).decrypt(nonce, ciphertext, _KEY_AAD)
        except (InvalidTag, KeyError, TypeError, ValueError) as error:
            raise ValueError("secret-store key failed authentication") from error
    key = base64.urlsafe_b64decode(encoded)
    # Transparently seal an existing raw data key when a KEK becomes available.
    if os.getenv("SECRET_STORE_PASSPHRASE"):
        temporary = config_root() / f".secret-store.key.seal.{os.getpid()}.tmp"
        _write_sealed_key(temporary, key)
        temporary.replace(config_root() / "secret-store.key")
    return key


def _secret_key() -> bytes:
    """Load or create the installation-local data encryption key."""
    path = config_root() / "secret-store.key"
    try:
        encoded = path.read_text(encoding="ascii").strip()
        key = _decode_key(encoded)
        if len(key) != 32:
            raise ValueError("invalid secret-store key length")
        return key
    except FileNotFoundError:
        pass

    root = config_root()
    root.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    temporary = root / f".secret-store.key.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    _write_sealed_key(temporary, key)
    try:
        # Do not silently replace a key created by another Core process.
        os.link(temporary, path)
        temporary.unlink()
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        return _secret_key()
    return key


def _read_encrypted_secrets() -> dict:
    path = config_root() / "secrets.enc.json"
    if not path.exists():
        return {}
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("encrypted secret store is unreadable") from exc
    if not isinstance(envelope, dict):
        raise ValueError("encrypted secret store envelope must be an object")
    if envelope.get("version") != 1 or envelope.get("algorithm") != "AES-256-GCM":
        raise ValueError("unsupported encrypted secret-store format")
    try:
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        plaintext = AESGCM(_secret_key()).decrypt(nonce, ciphertext, _SECRET_AAD)
        value = json.loads(plaintext)
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise ValueError("encrypted secret store failed authentication") from exc
    if not isinstance(value, dict):
        raise ValueError("encrypted secret store must contain an object")
    return value


def _write_encrypted_secrets(value: dict) -> None:
    nonce = os.urandom(12)
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ciphertext = AESGCM(_secret_key()).encrypt(nonce, plaintext, _SECRET_AAD)
    _write("secrets.enc.json", {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    })


def password_hash(value: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def ensure_secret_store() -> dict:
    with _secret_lock:
        stored = _read_encrypted_secrets()
        if stored:
            return stored
        # Transparently migrate both early appliance formats, then erase their files.
        stored = {**_read("secrets.json"), **_read("bootstrap-secrets.json")}
        generated = {name: secrets.token_urlsafe(size) for name, size in {
            "jwt_secret": 48, "worker_secret": 48,
            "internal_api_key": 48, "session_secret": 48}.items()}
        stored = {**generated, **stored}
        stored.pop("encryption_key", None)  # obsolete plaintext bootstrap key
        stored["created_at"] = stored.get("created_at", datetime.now(timezone.utc).isoformat())
        _write_encrypted_secrets(stored)
        for name in ("secrets.json", "bootstrap-secrets.json"):
            (config_root() / name).unlink(missing_ok=True)
        return stored


def installation() -> dict:
    return _read("installation.json")


def is_configured() -> bool:
    # Existing source installations remain usable; appliance installs explicitly set CONFIG_ROOT.
    if "CONFIG_ROOT" not in os.environ:
        return True
    return bool(installation().get("completed_at"))


def runtime_hardware() -> dict:
    detected = _read("hardware.json")
    return {"architecture": platform.machine(), "cpu_count": os.cpu_count(),
            "ram_mb": detected.get("ram_mb"), "gpu": detected.get("gpu", {"vendor": "none"}),
            **{key: value for key, value in detected.items() if key not in {"ram_mb", "gpu"}}}


def setup_status() -> dict:
    ensure_secret_store()
    return {"configured": is_configured(), "installation": installation() if is_configured() else None,
            "hardware": runtime_hardware(),
            "backends": ["ollama", "openai", "external", "skip"],
            "selected_role": os.getenv("NODE_ROLE") if os.getenv("NODE_ROLE") not in {None, "unassigned"} else None}


def complete_setup(name: str, username: str, password: str, confirmation: str,
                   backend: str, backend_url: str | None = None, node_role: str = "core",
                   core_url: str | None = None, node_credentials: dict | None = None) -> dict:
    if is_configured():
        raise FileExistsError("First-run setup has already been completed")
    if not name.strip() or len(name.strip()) > 120:
        raise ValueError("Installation name must contain 1-120 characters")
    if not username.replace("_", "").replace("-", "").isalnum() or not 3 <= len(username) <= 64:
        raise ValueError("Administrator login must contain 3-64 letters, numbers, '_' or '-'")
    if password != confirmation:
        raise ValueError("Password confirmation does not match")
    if len(password) < 12:
        raise ValueError("Administrator password must contain at least 12 characters")
    if backend not in {"ollama", "openai", "external", "skip"}:
        raise ValueError("Unsupported AI backend")
    from .node_registry import node_roles
    roles = node_roles()
    if node_role not in roles:
        raise ValueError("Unsupported node role")
    selected_role = os.getenv("NODE_ROLE")
    if selected_role not in {None, "unassigned", node_role}:
        raise ValueError(f"Bootstrap deployment plan is locked to role {selected_role}")
    if node_role != "core" and (not core_url or not node_credentials):
        raise ValueError("A non-Core node must be registered with Core")
    secrets_store = ensure_secret_store()
    value = {"installation_id": secrets.token_hex(16), "installation_name": name.strip(),
             "version": os.getenv("VERTEP_VERSION", "unknown"), "created_at": datetime.now(timezone.utc).isoformat(),
             "completed_at": datetime.now(timezone.utc).isoformat(), "hardware": runtime_hardware(),
             "node_role": node_role, "modules": roles[node_role]["modules"],
             "core_url": core_url if node_role != "core" else None,
             "worker_count": int(os.getenv("BOOTSTRAP_WORKER_COUNT", "0")),
             "ai_backend": {"type": backend, "url": backend_url},
             "administrator": {"username": username, "password_hash": password_hash(password), "role": "admin"}}
    _write("installation.json", value)
    if node_credentials:
        _write("node-credentials.json", node_credentials)
    return {key: item for key, item in value.items() if key != "administrator"} | {
        "administrator": {"username": username, "role": "admin"},
        "session_secret_ready": bool(secrets_store.get("session_secret"))}


def configured_user() -> tuple[str, dict] | None:
    admin = installation().get("administrator")
    return (admin.get("username"), admin) if isinstance(admin, dict) else None


def session_secret() -> str:
    if "CONFIG_ROOT" not in os.environ:
        return os.getenv("ADMIN_PASSWORD", "")
    return str(ensure_secret_store().get("session_secret", ""))


def integration_secret_status() -> dict[str, bool]:
    """Return presence metadata only; secret values never cross the API boundary."""
    stored = ensure_secret_store()
    return {name: bool(stored.get(name)) for name in sorted(INTEGRATION_SECRET_NAMES)}


def set_integration_secret(name: str, value: str | None) -> dict[str, bool]:
    if name not in INTEGRATION_SECRET_NAMES:
        raise ValueError("Unsupported integration secret")
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 16_384):
        raise ValueError("Secret must contain 1-16384 characters")
    with _secret_lock:
        stored = ensure_secret_store()
        if value is None:
            stored.pop(name, None)
        else:
            stored[name] = value
        stored["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_encrypted_secrets(stored)
        return {key: bool(stored.get(key)) for key in sorted(INTEGRATION_SECRET_NAMES)}
