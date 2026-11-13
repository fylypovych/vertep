"""Node enrollment: CSR, mTLS credential request with retries and renewal."""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx


def _ensure_csr(node_name: str, pki: Path, csr_path: Path | None = None) -> str:
    """Generate the node key locally and return only its CSR for Core signing.

    The ``csr_provider`` argument lets tests supply a stub instead of the
    real ``openssl`` binary, so the enrollment retry policy can be exercised
    without a PKI toolchain on the test host.
    """
    csr_path = csr_path or pki / "node.csr"
    if csr_path.is_file():
        return csr_path.read_text(encoding="utf-8")
    csr_provider = os.getenv("CSR_PROVIDER")
    if csr_provider:
        return csr_provider  # tests inject a fixed CSR string
    key, csr = pki / "node.key", pki / "node.csr"
    if not key.exists():
        subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={node_name}",
                        "-keyout", str(key), "-out", str(csr)], check=True, capture_output=True)
        os.chmod(key, 0o600)
    elif not csr.exists():
        subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                        "-out", str(csr)], check=True, capture_output=True)
    return csr.read_text(encoding="utf-8")


def _enrollment_id(pki: Path) -> str:
    """Return a stable, locally persisted enrollment id used as a retry key.

    The key survives process restarts so a node whose enrollment response was
    lost can retry idempotently instead of deadlocking on its own burned
    single-use token.
    """
    path = pki / "enrollment.id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", existing):
            return existing
    except OSError:
        pass
    value = f"{node_id_slug()}-{secrets.token_hex(8)}"
    pki.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return value


def node_id_slug() -> str:
    """Node id prefix used for the locally generated enrollment id."""
    return re.sub(r"[^a-z0-9-]", "-", str(os.getenv("NODE_NAME", "vertep-node")).lower()).strip("-") or "vertep-node"


def _retry_enrollment(client: httpx.Client, core: str, node_name: str, metrics: dict,
                     capabilities: list[str], config_path: Path | None = None) -> str:
    """Submit the enrollment request with bounded lost-response retry.

    Disposable nodes (single-use registration tokens, short-lived CSR) must not
    be lost because a transient transport failure arrived between the local key
    generation and the Core acknowledgement.  Two failure classes are handled:

    * transport-level failures and transient 5xx/429 are retried, because the
      request may never have reached Core;
    * a lost *response* — Core committed the enrollment but the answer never
      arrived — is retried with the same ``enrollment_id`` idempotency key, so
      Core re-issues credentials for this node instead of rejecting the burned
      token.

    A rejected token, a malformed response or any other permanent 4xx is
    surfaced immediately so the operator sees the real problem instead of a
    retry loop.
    """
    config_path = Path(config_path or os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    registration_token = os.getenv("REGISTRATION_TOKEN", "")
    if not registration_token:
        return os.getenv("NODE_API_TOKEN", "")
    memory_mb = None
    try:
        memory_mb = int(Path("/proc/meminfo").read_text().split("MemTotal:", 1)[1].split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    pki = config_path.parent / "pki"
    pki.mkdir(parents=True, exist_ok=True)
    csr = _ensure_csr(node_name, pki)
    enrollment_id = _enrollment_id(pki)

    max_attempts = max(1, int(os.getenv("ENROLLMENT_RETRY_ATTEMPTS", "5")))
    base_delay = max(0.05, float(os.getenv("ENROLLMENT_RETRY_BASE_SECONDS", "0.5")))
    transient_statuses = {429, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(f"{core}/api/nodes/register", json={
                "registration_token": registration_token, "node_id": node_name,
                "enrollment_id": enrollment_id,
                "capabilities": capabilities, "version": os.getenv("VERTEP_VERSION", "unknown"),
                "csr": csr,
                "hardware": {**metrics, "ram_mb": memory_mb,
                             "disk_free_mb": shutil.disk_usage("/").free // 1024 // 1024}}, timeout=30)
            status = getattr(response, "status_code", 200)
            if status in transient_statuses and attempt < max_attempts:
                raise httpx.HTTPStatusError(
                    f"transient enrollment status {status}",
                    request=getattr(response, "request", None), response=response)
            if status >= 400:
                # Permanent client errors (invalid token, malformed request) are
                # surfaced immediately: retrying them would only burn the
                # single-use token and confuse the operator about the real cause.
                raise httpx.HTTPStatusError(
                    f"enrollment rejected with status {status}",
                    request=getattr(response, "request", None), response=response)
            credentials = response.json()
            (pki / "node.crt").write_text(credentials["certificate"], encoding="utf-8")
            (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = config_path.with_name(f".{config_path.name}.tmp")
            temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(config_path)
            return str(credentials["jwt"])
        except httpx.HTTPStatusError as error:
            # Permanent 4xx must never be retried: only transient 5xx/429 and
            # transport-level failures reach the retry path below.
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and status not in transient_statuses:
                raise
            last_error = error
            if attempt >= max_attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            if attempt >= max_attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
    # Unreachable: the loop always raises or returns.
    raise last_error if last_error else RuntimeError("enrollment produced no result")  # pragma: no cover


def enroll(client: httpx.Client, core: str, node_name: str, metrics: dict,
           capabilities: list[str]) -> str:
    config_path = Path(os.getenv("NODE_CONFIG_PATH", "/data/config/node-credentials.json"))
    try:
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        if stored.get("jwt"):
            return str(stored["jwt"])
    except (OSError, ValueError):
        pass
    return _retry_enrollment(client, core, node_name, metrics, capabilities, config_path)

def renew_if_needed(core: str, node_name: str, credential: str, verify: str | bool,
                    pki: Path, config_path: Path) -> str:
    certificate, key, csr = pki / "node.crt", pki / "node.key", pki / "node.csr"
    if not certificate.is_file() or not key.is_file():
        return credential
    threshold = int(os.getenv("CERTIFICATE_RENEW_BEFORE_SECONDS", str(30 * 86400)))
    valid = subprocess.run(["openssl", "x509", "-checkend", str(threshold), "-noout",
                            "-in", str(certificate)], capture_output=True, check=False).returncode == 0
    if valid:
        return credential
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={node_name}",
                    "-out", str(csr)], check=True, capture_output=True)
    try:
        renewal_credential = json.loads(config_path.read_text(encoding="utf-8")).get("worker_secret") or credential
    except (OSError, ValueError):
        renewal_credential = credential
    with httpx.Client(timeout=30, verify=verify, cert=(str(certificate), str(key)),
                      headers={"X-Vertep-Token": renewal_credential}) as client:
        response = client.post(f"{core}/api/nodes/{node_name}/renew",
                               json={"csr": csr.read_text(encoding="utf-8")})
        response.raise_for_status()
    credentials = response.json()
    certificate.write_text(credentials["certificate"], encoding="utf-8")
    (pki / "node-ca.crt").write_text(credentials["core_certificate"], encoding="utf-8")
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    temporary.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    return str(credentials["jwt"])

# CORE no longer wants this attempt: the Job was deleted (404) or the task was
# reassigned (409). Repeating the request can never succeed.
