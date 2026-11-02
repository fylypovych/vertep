#!/usr/bin/env python3
"""Debug the failing test."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import tarfile
import threading
import ssl
import base64
import hashlib
import ipaddress
import datetime
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography import x509
from cryptography.x509.oid import NameOID

ROOT = Path("D:/prj/vertep")

# Load the modules
spec = importlib.util.spec_from_file_location('core_rollout_coordinator', ROOT / 'scripts' / 'core-rollout-coordinator.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

spec = importlib.util.spec_from_file_location('update_agent', ROOT / 'scripts' / 'update-agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

# Create a temporary directory for the test
with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_path = Path(tmp_dir)
    print(f'Temp dir: {tmp_path}')

    # Build disposable deployment
    scripts = tmp_path / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)

    # Write host consumer
    host_consumer = '''#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _layout():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "release_layout", ROOT / "scripts" / "release-layout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _activation() -> dict:
    path = ROOT / ".activation.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}

def _extract(package: Path, staging: Path) -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "safe-extract.py"),
                    str(package), str(staging)], check=True)
    if (staging / "VERSION").is_file():
        return staging
    children = sorted(item for item in staging.iterdir() if item.is_dir())
    if len(children) != 1:
        raise SystemExit("release payload is incomplete")
    return children[0]

def _prepare(payload: Path, version: str) -> None:
    releases = ROOT / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / version
    if release.exists():
        raise SystemExit(f"release already exists: {version}")
    try:
        _layout().prepare(ROOT, payload, version)
        return
    except OSError:
        shutil.rmtree(release, ignore_errors=True)
    shutil.copytree(payload, release)
    (release / ".release.json").write_text(
        json.dumps({"version": version, "sha256": ""}), encoding="utf-8")

def _activate_by_copy(version: str) -> None:
    release = ROOT / "releases" / version
    staged = ROOT / f".current.{os.getpid()}.tmp"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(release, staged)
    current = ROOT / "current"
    replaced = ROOT / f".current.{os.getpid()}.previous"
    if current.exists():
        os.replace(current, replaced)
    os.replace(staged, current)
    shutil.rmtree(replaced, ignore_errors=True)

def _activate(version: str, previous) -> None:
    release = ROOT / "releases" / version
    if not (release / "VERSION").is_file():
        raise SystemExit("release is missing")
    try:
        _layout().activate(ROOT, version)
    except OSError:
        _activate_by_copy(version)
    shutil.copyfile(ROOT / "current" / "VERSION", ROOT / "VERSION")
    (ROOT / ".activation.json").write_text(
        json.dumps({"current": version, "previous": previous}), encoding="utf-8")

def _runtime_record() -> dict:
    try:
        value = json.loads((ROOT / "runtime" / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def _probe(record: dict):
    import urllib.request
    if not record.get("port"):
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{record['port']}/api/health", timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None

def _stop_runtime() -> None:
    import signal
    import time
    record = _runtime_record()
    if not record.get("pid") or _probe(record) is None:
        return
    try:
        os.kill(int(record["pid"]), signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 15
    while _probe(record) is not None:
        if time.monotonic() > deadline:
            raise SystemExit("running runtime instance did not stop")
        time.sleep(0.05)

def _operation_id() -> str:
    status = os.environ.get("VERTEP_UPDATE_STATUS_FILE")
    try:
        return str(json.loads(Path(status).read_text(encoding="utf-8")).get("request_id") or "")
    except (OSError, TypeError, ValueError):
        return ""

def _start_runtime() -> dict:
    import time
    previous = _runtime_record().get("instance")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "vertep-runtime.py"), str(ROOT), _operation_id()],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt", creationflags=flags)
    expected = (ROOT / "current" / "VERSION").read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = _runtime_record()
        health = _probe(record) if record.get("instance") != previous else None
        if health and health.get("version") == expected:
            return health
        time.sleep(0.05)
    raise SystemExit("runtime health check failed")

def _restart_runtime() -> None:
    delay = ROOT / "restart-delay"
    if delay.is_file():
        import time
        time.sleep(float(delay.read_text(encoding="utf-8") or 0))
    _stop_runtime()
    _start_runtime()

def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "apply-update":
        package = Path(sys.argv[2])
        version = sys.argv[3]
        if (ROOT / "fail-apply").exists():
            raise SystemExit(7)
        staging = ROOT / f".staging.{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        payload = _extract(package, staging)
        _prepare(payload, version)
        _activate(version, _activation().get("current"))
        shutil.rmtree(staging, ignore_errors=True)
        if (ROOT / "fail-migration").exists():
            raise SystemExit(9)
        _restart_runtime()
    elif action == "rollback":
        previous = _activation().get("previous")
        if not previous:
            raise SystemExit("no immutable previous release is available")
        _activate(previous, None)
        _restart_runtime()
    elif action == "restart-runtime":
        _restart_runtime()
    elif action == "start-runtime":
        _start_runtime()
    elif action == "stop-runtime":
        _stop_runtime()
    else:
        raise SystemExit(f"unsupported action: {action}")

if __name__ == "__main__":
    main()
'''
    (scripts / 'vertep-host.py').write_text(host_consumer, encoding='utf-8')
    (scripts / 'vertep').write_text(host_consumer, encoding='utf-8')

    # Copy required scripts
    shutil.copyfile(ROOT / 'scripts' / 'safe-extract.py', scripts / 'safe-extract.py')
    shutil.copyfile(ROOT / 'scripts' / 'release-layout.py', scripts / 'release-layout.py')

    # Create runtime script
    runtime_script = '''#!/usr/bin/env python3
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(sys.argv[1])
RECORD = {"instance": uuid.uuid4().hex, "pid": os.getpid(),
          "version": (ROOT / "current" / "VERSION").read_text(encoding="utf-8").strip(),
          "operation_id": sys.argv[2] if len(sys.argv) > 2 else "",
          "started_at": datetime.now(timezone.utc).isoformat()}

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", **RECORD}).encode("utf-8")
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        return None

server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
RECORD["port"] = server.server_address[1]
runtime = ROOT / "runtime"
runtime.mkdir(exist_ok=True)
temporary = runtime / f".instance.{os.getpid()}.tmp"
temporary.write_text(json.dumps(RECORD), encoding="utf-8")
os.replace(temporary, runtime / "instance.json")
server.serve_forever()
'''
    (scripts / 'vertep-runtime.py').write_text(runtime_script, encoding='utf-8')

    # Create release
    release = tmp_path / 'releases' / '0.0.0.1'
    release.mkdir(parents=True, exist_ok=True)
    (release / 'VERSION').write_text('0.0.0.1', encoding='utf-8')
    (release / 'application.txt').write_text('runtime 0.0.0.1', encoding='utf-8')
    (release / '.release.json').write_text(json.dumps({'version': '0.0.0.1', 'sha256': ''}), encoding='utf-8')
    (tmp_path / 'VERSION').write_text('0.0.0.1', encoding='utf-8')

    current = tmp_path / 'current'
    shutil.copytree(release, current)

    (tmp_path / '.activation.json').write_text(json.dumps({'current': '0.0.0.1', 'previous': None}), encoding='utf-8')

    # Start runtime
    print('Starting runtime...')
    result = subprocess.run([sys.executable, str(scripts / 'vertep-host.py'), 'start-runtime'], check=True, timeout=60)
    print('Runtime started')

    # Create state dir
    state_dir = tmp_path / 'state'
    state_dir.mkdir()

    # Set environment
    os.environ['VERTEP_ROOT'] = str(tmp_path)
    os.environ['UPDATE_STATE_DIR'] = str(state_dir)
    os.environ['SYSTEM_STATE_BACKEND'] = 'file'
    os.environ['NODE_ADDITIONAL_ROLES'] = 'backup'
    os.environ['BACKUP_ROOT'] = str(state_dir / 'backups')

    # Create SignedUpdateServer (from conftest.py)
    class SignedUpdateServer:
        def __init__(self, root: Path):
            self.root = Path(root)
            self.files = self.root / "origin"
            self.files.mkdir(parents=True, exist_ok=True)
            self.sequence = 0
            self._signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.public_key = self.root / "update-public.pem"
            self.public_key.write_bytes(self._signing_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                           .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(now - datetime.timedelta(minutes=5))
                           .not_valid_after(now + datetime.timedelta(days=1))
                           .add_extension(x509.SubjectAlternativeName(
                               [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                           .sign(tls_key, hashes.SHA256()))
            self.certificate = self.root / "update-origin.crt"
            self.certificate.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            tls_key_path = self.root / "update-origin.key"
            tls_key_path.write_bytes(tls_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.certificate, tls_key_path)

            class Quiet(SimpleHTTPRequestHandler):
                def log_message(self, *_args):
                    return None

            self._server = ThreadingHTTPServer(
                ("127.0.0.1", 0), lambda *a, **kw: Quiet(*a, directory=str(self.files), **kw))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            self.url = f"https://127.0.0.1:{self._server.server_address[1]}"
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

        def environment(self) -> dict[str, str]:
            return {"VERTEP_UPDATE_SERVER": self.url, "SSL_CERT_FILE": str(self.certificate),
                    "UPDATE_PUBLIC_KEY": str(self.public_key), "UPDATE_CHANNEL": "stable",
                    "UPDATE_HTTP_RETRIES": "1"}

        def publish(self, version: str, package: Path, *, tamper: bool = False) -> dict:
            from core.update_protocol import canonical_manifest
            self.sequence += 1
            relative = f"v1/packages/vertep-{version}.tar.gz"
            target = self.files / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package, target)
            manifest = {"version": version, "channel": "stable", "required": False,
                        "package": relative, "release_sequence": self.sequence,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            manifest["signature"] = base64.b64encode(self._signing_key.sign(
                canonical_manifest(manifest), padding.PKCS1v15(), hashes.SHA256())).decode()
            if tamper:
                manifest["sha256"] = "0" * 64
            latest = self.files / "v1" / "releases" / "stable" / "latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps(manifest), encoding="utf-8")
            return manifest

        def close(self) -> None:
            self._server.shutdown()
            self._server.server_close()

    # Create and start update server
    print('Starting update server...')
    server = SignedUpdateServer(tmp_path / "update-origin")
    for key, value in server.environment().items():
        os.environ[key] = value
    print(f'Update server URL: {server.url}')

    # Create release package
    payload = tmp_path / 'payload-0.0.0.2'
    payload.mkdir(parents=True, exist_ok=True)
    (payload / 'VERSION').write_text('0.0.0.2', encoding='utf-8')
    (payload / 'application.txt').write_text('runtime 0.0.0.2', encoding='utf-8')
    archive = tmp_path / 'vertep-0.0.0.2.tar.gz'
    with tarfile.open(archive, 'w:gz') as package:
        package.add(payload, arcname=payload.name)

    # Publish the release to the update server
    server.publish("0.0.0.2", archive)
    print(f'Release published to update server')

    # Test the agent
    try:
        request_id = 'a' * 32
        request_file = state_dir / 'requests' / f'{request_id}.json'
        request_file.parent.mkdir(parents=True)
        request_file.write_text(json.dumps({'request_id': request_id, 'action': 'update', 'target_version': '0.0.0.2'}), encoding='utf-8')

        print('Calling process_request...')
        result = agent.process_request(tmp_path, state_dir, request_file, skip_drain=True)
        print(f'Result: {result}')

        if not result:
            # Check status.json for error
            status = json.loads((state_dir / 'status.json').read_text(encoding='utf-8'))
            print(f'Status: {json.dumps(status, indent=2)}')
    except Exception as e:
        import traceback
        print(f'Exception: {e}')
        traceback.print_exc()