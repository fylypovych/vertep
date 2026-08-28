"""One-time enrollment and durable credentials for self-registering nodes."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from datetime import datetime, timezone
from pathlib import Path

from .first_run import config_root, ensure_secret_store


_lock = threading.RLock()
NODE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def _postgres_enabled() -> bool:
    return os.getenv("NODE_REGISTRY_BACKEND", "file").lower() == "postgres"


def _connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"])


def _public_node(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "secret_hash"}


def _path() -> Path:
    return config_root() / "node-registry.json"


def _load() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"tokens": {}, "nodes": {}, "revoked_serials": []}
    except (OSError, ValueError):
        return {"tokens": {}, "nodes": {}, "revoked_serials": []}
        return value if isinstance(value, dict) else {"tokens": {}, "nodes": {}}
    except (OSError, ValueError):
        return {"tokens": {}, "nodes": {}}


def _save(value: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _token_hash(token: str) -> str:
    key = ensure_secret_store()["internal_api_key"].encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def create_registration_token(role: str, ttl_seconds: int = 900) -> dict:
    if role not in node_roles() or role == "core":
        raise ValueError("Unsupported registration role")
    if not 60 <= ttl_seconds <= 3600:
        raise ValueError("Registration token lifetime must be between 60 and 3600 seconds")
    raw = "VT-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
    expires_at = time.time() + ttl_seconds
    if _postgres_enabled():
        with _connect() as connection:
            connection.execute("DELETE FROM node_registration_tokens WHERE expires_at <= now()")
            connection.execute("""INSERT INTO node_registration_tokens(token_hash,role,expires_at)
                                   VALUES(%s,%s,to_timestamp(%s))""", (_token_hash(raw), role, expires_at))
    else:
        with _lock:
            registry = _load()
            registry["tokens"][_token_hash(raw)] = {"role": role, "expires_at": expires_at,
                                                      "created_at": datetime.now(timezone.utc).isoformat()}
            _save(registry)
    return {"token": raw, "role": role, "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat()}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def issue_node_token(node_id: str, role: str, generation: int = 1) -> str:
    now = int(time.time())
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64(json.dumps({"sub": node_id, "role": role, "iat": now, "nbf": now - 5,
                               "exp": now + int(os.getenv("NODE_TOKEN_TTL_SECONDS", "86400")),
                               "iss": "vertep-core", "aud": "vertep-node-api",
                               "jti": secrets.token_hex(16), "generation": generation,
                               "type": "vertep-node"}, separators=(",", ":")).encode())
    signature = hmac.new(ensure_secret_store()["worker_secret"].encode(),
                         f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def verify_node_token(token: str, node_id: str) -> bool:
    try:
        header, payload, supplied = token.split(".")
        expected = _b64(hmac.new(ensure_secret_store()["worker_secret"].encode(),
                                f"{header}.{payload}".encode(), hashlib.sha256).digest())
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        record = _node_record(node_id)
        now = int(time.time())
        return (secrets.compare_digest(expected, supplied) and decoded.get("sub") == node_id
                and decoded.get("type") == "vertep-node" and decoded.get("iss") == "vertep-core"
                and decoded.get("aud") == "vertep-node-api" and int(decoded.get("nbf", now + 1)) <= now
                and int(decoded.get("exp", 0)) > now and record.get("status") != "REVOKED"
                and decoded.get("generation") == record.get("credential_generation"))
    except (ValueError, TypeError, json.JSONDecodeError):
        record = _node_record(node_id)
        secret_hash = record.get("secret_hash", "")
        return (record.get("status") != "REVOKED" and bool(secret_hash)
                and secrets.compare_digest(secret_hash, _token_hash(token)))


def verify_node_certificate(node_id: str, supplied_serial: str) -> bool:
    """Bind an authenticated TLS client to the currently issued registry certificate."""
    if not supplied_serial:
        return False
    record = _node_record(node_id)
    expected = str(record.get("certificate_serial", "")).upper().lstrip("0") or "0"
    actual = str(supplied_serial).strip().upper().removeprefix("0X").lstrip("0") or "0"
    return (record.get("status") != "REVOKED" and bool(record.get("certificate_serial"))
            and secrets.compare_digest(expected, actual))


def enroll_node(token: str, requested_id: str, capabilities: list[str], hardware: dict,
                version: str, csr: str) -> dict:
    node_id = requested_id.strip().lower()
    if not NODE_ID.fullmatch(node_id):
        raise ValueError("Node ID must contain 3-64 lowercase letters, numbers or hyphens")
    if not isinstance(csr, str) or len(csr) > 16384 or "BEGIN CERTIFICATE REQUEST" not in csr:
        raise ValueError("A PEM certificate signing request is required")
    clean_capabilities = sorted({item for item in capabilities
                                 if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", item)})
    if _postgres_enabled():
        return _enroll_postgres(token, node_id, clean_capabilities, hardware, version, csr)
    with _lock:
        registry = _load()
        token_key = _token_hash(token)
        token_record = registry["tokens"].get(token_key)
        if not token_record or token_record["expires_at"] < time.time():
            raise PermissionError("Registration token is invalid, expired, or already used")
        role = token_record["role"]
        allowed = set(node_roles()[role]["capabilities"])
        effective = sorted(set(clean_capabilities) & allowed) if clean_capabilities else sorted(allowed)
        if not effective:
            raise ValueError("Node did not report any capability allowed for its registration role")
        node_secret = secrets.token_urlsafe(48)
        certificate, core_certificate, certificate_serial, certificate_expires_at = _issue_certificate(node_id, csr)
        registry["tokens"].pop(token_key)
        registry["nodes"][node_id] = {"node_id": node_id, "role": role,
            "capabilities": effective, "hardware": hardware, "version": version,
            "secret_hash": _token_hash(node_secret),
            "status": "READY", "credential_generation": 1,
            "certificate_serial": certificate_serial, "certificate_expires_at": certificate_expires_at,
            "registered_at": datetime.now(timezone.utc).isoformat()}
        _save(registry)
    jwt = issue_node_token(node_id, role, 1)
    return {"worker_id": node_id, "role": role, "status": "READY", "jwt": jwt,
            "worker_secret": node_secret,
            "certificate": certificate, "core_certificate": core_certificate,
            "configuration": {"capabilities": effective, "heartbeat_seconds": 15}}


def registered_nodes() -> list[dict]:
    if _postgres_enabled():
        from psycopg.rows import dict_row
        with _connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute("""SELECT node_id,role,capabilities,hardware,version,status,
                    credential_generation,certificate_serial,certificate_expires_at,
                    registered_at,revoked_at FROM registered_nodes ORDER BY registered_at""").fetchall()
        return [dict(row) for row in rows]
    return [{key: value for key, value in node.items() if key != "secret_hash"}
            for node in _load()["nodes"].values()]


def revoke_node(node_id: str) -> dict:
    if _postgres_enabled():
        from psycopg.rows import dict_row
        with _connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute("""UPDATE registered_nodes SET status='REVOKED',
                    credential_generation=credential_generation+1,revoked_at=now() WHERE node_id=%s
                    RETURNING node_id,role,capabilities,hardware,version,status,credential_generation,
                    certificate_serial,certificate_expires_at,registered_at,revoked_at""",
                    (node_id,)).fetchone()
                if row:
                    _record_revoked_serial(row.get("certificate_serial"), row.get("revoked_at"), connection)
        if not row:
            raise KeyError(node_id)
        value = dict(row)
        write_node_crl()
        return value
        if not row:
            raise KeyError(node_id)
        return dict(row)
    with _lock:
        registry = _load()
        node = registry.get("nodes", {}).get(node_id)
        if not node:
            raise KeyError(node_id)
        node["status"] = "REVOKED"
        node["credential_generation"] = int(node.get("credential_generation", 1)) + 1
        node["revoked_at"] = datetime.now(timezone.utc).isoformat()
        registry.setdefault("revoked_serials", []).append({"serial": node.get("certificate_serial"),
                                                            "revoked_at": node["revoked_at"]})
        _save(registry)
        result = {key: value for key, value in node.items() if key != "secret_hash"}
    write_node_crl()
    return result
        _save(registry)
        return {key: value for key, value in node.items() if key != "secret_hash"}


def renew_node(node_id: str, csr: str) -> dict:
    if not isinstance(csr, str) or len(csr) > 16384 or "BEGIN CERTIFICATE REQUEST" not in csr:
        raise ValueError("A PEM certificate signing request is required")
    certificate, ca_certificate, serial, expires = _issue_certificate(node_id, csr)
    node_secret = secrets.token_urlsafe(48)
    if _postgres_enabled():
        with _connect() as connection:
            previous = connection.execute("SELECT certificate_serial FROM registered_nodes WHERE node_id=%s",
                                          (node_id,)).fetchone()
            row = connection.execute("""UPDATE registered_nodes SET secret_hash=%s,
                credential_generation=credential_generation+1,certificate_serial=%s,
                certificate_expires_at=%s WHERE node_id=%s AND status<>'REVOKED'
                RETURNING role,credential_generation,capabilities""",
                (_token_hash(node_secret), serial, expires, node_id)).fetchone()
            if row and previous and previous[0]:
                _record_revoked_serial(previous[0], datetime.now(timezone.utc), connection)
        if not row:
            raise KeyError(node_id)
        role, generation, capabilities = row
    else:
        with _lock:
            registry = _load()
            node = registry.get("nodes", {}).get(node_id)
            if not node or node.get("status") == "REVOKED":
                raise KeyError(node_id)
            previous_serial = node.get("certificate_serial")
            node["secret_hash"] = _token_hash(node_secret)
            node["credential_generation"] = int(node.get("credential_generation", 1)) + 1
            node["certificate_serial"] = serial
            node["certificate_expires_at"] = expires
            if previous_serial:
                registry.setdefault("revoked_serials", []).append({
                    "serial": previous_serial, "revoked_at": datetime.now(timezone.utc).isoformat()})
            _save(registry)
            role, generation, capabilities = node["role"], node["credential_generation"], node["capabilities"]
    write_node_crl()
            _save(registry)
            role, generation, capabilities = node["role"], node["credential_generation"], node["capabilities"]
    return {"worker_id": node_id, "role": role, "status": "READY",
            "jwt": issue_node_token(node_id, role, generation), "worker_secret": node_secret,
            "certificate": certificate, "core_certificate": ca_certificate,
            "configuration": {"capabilities": capabilities, "heartbeat_seconds": 15}}


def node_roles() -> dict:
    path = Path(os.getenv("NODE_ROLES_FILE", "config/node_roles.json"))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _node_record(node_id: str) -> dict:
    if not _postgres_enabled():
        return _load().get("nodes", {}).get(node_id, {})
    from psycopg.rows import dict_row
    with _connect() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute("""SELECT node_id,status,credential_generation,secret_hash,
                certificate_serial,certificate_expires_at
            row = cursor.execute("""SELECT node_id,status,credential_generation,secret_hash
                FROM registered_nodes WHERE node_id=%s""", (node_id,)).fetchone()
    return dict(row) if row else {}


def _enroll_postgres(token: str, node_id: str, clean_capabilities: list[str], hardware: dict,
                     version: str, csr: str) -> dict:
    import json as json_module
    with _connect() as connection:
        token_record = connection.execute("""DELETE FROM node_registration_tokens
            WHERE token_hash=%s AND expires_at>now() RETURNING role""", (_token_hash(token),)).fetchone()
        if not token_record:
            raise PermissionError("Registration token is invalid, expired, or already used")
        role = token_record[0]
        allowed = set(node_roles()[role]["capabilities"])
        effective = sorted(set(clean_capabilities) & allowed) if clean_capabilities else sorted(allowed)
        if not effective:
            raise ValueError("Node did not report any capability allowed for its registration role")
        node_secret = secrets.token_urlsafe(48)
        certificate, core_certificate, serial, expires = _issue_certificate(node_id, csr)
        connection.execute("""INSERT INTO registered_nodes(node_id,role,capabilities,hardware,version,
            secret_hash,status,credential_generation,certificate_serial,certificate_expires_at)
            VALUES(%s,%s,%s::jsonb,%s::jsonb,%s,%s,'READY',1,%s,%s)
            ON CONFLICT(node_id) DO UPDATE SET role=excluded.role,capabilities=excluded.capabilities,
            hardware=excluded.hardware,version=excluded.version,secret_hash=excluded.secret_hash,status='READY',
            credential_generation=registered_nodes.credential_generation+1,
            certificate_serial=excluded.certificate_serial,certificate_expires_at=excluded.certificate_expires_at,
            registered_at=now(),revoked_at=NULL""",
            (node_id, role, json_module.dumps(effective), json_module.dumps(hardware), version,
             _token_hash(node_secret), serial, expires))
        generation = connection.execute("SELECT credential_generation FROM registered_nodes WHERE node_id=%s",
                                        (node_id,)).fetchone()[0]
    return {"worker_id": node_id, "role": role, "status": "READY",
            "jwt": issue_node_token(node_id, role, generation), "worker_secret": node_secret,
            "certificate": certificate, "core_certificate": core_certificate,
            "configuration": {"capabilities": effective, "heartbeat_seconds": 15}}


def create_node_csr(node_id: str, directory: Path | None = None) -> str:
    """Generate the node key locally and return only its CSR for Core signing."""
    if not NODE_ID.fullmatch(node_id):
        raise ValueError("Invalid node ID")
    directory = directory or config_root() / "pki"
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "node.key"
    csr = directory / "node.csr"
    if not key.exists():
        subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={node_id}",
                        "-keyout", str(key), "-out", str(csr)], check=True, capture_output=True)
        os.chmod(key, 0o600)
    elif not csr.exists():
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_id}",
                        "-out", str(csr)], check=True, capture_output=True)
    return csr.read_text(encoding="utf-8")


def _issue_certificate(node_id: str, csr_pem: str) -> tuple[str, str, str, str]:
    """Create the installation CA once and issue a short-lived node TLS certificate."""
    with _lock:
        tls = config_root() / "pki"
        tls.mkdir(parents=True, exist_ok=True)
        ca_key = Path(os.getenv("NODE_CA_KEY_PATH", str(tls / "ca.key")))
        ca_cert = Path(os.getenv("NODE_CA_CERT_PATH", str(tls / "ca.crt")))
        if not ca_key.exists() or not ca_cert.exists():
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
                            "-subj", "/CN=Vertep Installation CA", "-keyout", str(ca_key), "-out", str(ca_cert)],
                           check=True, capture_output=True)
            os.chmod(ca_key, 0o600)
        with tempfile.TemporaryDirectory() as temporary:
            csr, certificate, extensions = (Path(temporary) / name for name in ("node.csr", "node.crt", "extensions.cnf"))
            csr.write_text(csr_pem, encoding="utf-8")
            check = subprocess.run(["openssl", "req", "-in", str(csr), "-verify", "-noout"],
                                   check=False, capture_output=True, text=True)
            if check.returncode:
                raise ValueError("Certificate signing request signature is invalid")
            extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                  f"extendedKeyUsage=clientAuth\nsubjectAltName=URI:spiffe://vertep/node/{node_id}\n",
                                  encoding="utf-8")
            subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
                            "-CAkey", str(ca_key), "-set_serial", str(secrets.randbits(159) or 1),
                            "-days", "397", "-sha256",
                            "-CAkey", str(ca_key), "-CAcreateserial", "-days", "397", "-sha256",
                            "-extfile", str(extensions), "-out", str(certificate)], check=True, capture_output=True)
            metadata = subprocess.run(["openssl", "x509", "-in", str(certificate), "-noout", "-serial", "-enddate"],
                                      check=True, capture_output=True, text=True).stdout.splitlines()
            serial = metadata[0].split("=", 1)[1]
            expires = metadata[1].split("=", 1)[1]
            return certificate.read_text(), ca_cert.read_text(), serial, expires


def _record_revoked_serial(serial: str | None, revoked_at, connection=None) -> None:
    if not serial:
        return
    if connection is not None:
        connection.execute("""INSERT INTO node_revoked_certificates(serial,revoked_at)
                            VALUES(%s,%s) ON CONFLICT(serial) DO NOTHING""", (serial, revoked_at))
        return
    with _connect() as local:
        local.execute("""INSERT INTO node_revoked_certificates(serial,revoked_at)
                       VALUES(%s,%s) ON CONFLICT(serial) DO NOTHING""", (serial, revoked_at))


def _revoked_certificates() -> list[dict]:
    if _postgres_enabled():
        with _connect() as connection:
            rows = connection.execute("SELECT serial,revoked_at FROM node_revoked_certificates").fetchall()
        return [{"serial": row[0], "revoked_at": row[1]} for row in rows]
    return _load().get("revoked_serials", [])


def write_node_crl() -> Path:
    """Atomically publish a CA-signed CRL consumed by the TLS proxy."""
    ca_key_path = Path(os.getenv("NODE_CA_KEY_PATH", str(config_root() / "pki/ca.key")))
    ca_cert_path = Path(os.getenv("NODE_CA_CERT_PATH", str(config_root() / "pki/ca.crt")))
    output_path = Path(os.getenv("NODE_CRL_PATH", str(config_root() / "node-ca.crl")))
    certificate = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    private_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    now = datetime.now(timezone.utc)
    builder = x509.CertificateRevocationListBuilder().issuer_name(certificate.subject)
    builder = builder.last_update(now).next_update(now + timedelta(days=1))
    for item in _revoked_certificates():
        try:
            revoked_at = item["revoked_at"]
            if isinstance(revoked_at, str):
                revoked_at = datetime.fromisoformat(revoked_at.replace("Z", "+00:00"))
            revoked = x509.RevokedCertificateBuilder().serial_number(
                int(str(item["serial"]), 16)).revocation_date(revoked_at).build()
            builder = builder.add_revoked_certificate(revoked)
        except (KeyError, TypeError, ValueError):
            continue
    encoded = builder.sign(private_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded)
    os.chmod(temporary, 0o644)
    temporary.replace(output_path)
    return output_path
