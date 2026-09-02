import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import _hash_secret, app
from core.update_manager import request_update, update_status


def load_update_agent():
    path = Path("scripts/update-agent.py").resolve()
    spec = importlib.util.spec_from_file_location("vertep_update_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_update_manifest_builder():
    path = Path("scripts/build-update-manifest.py").resolve()
    spec = importlib.util.spec_from_file_location("build_update_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_runtime_env_updater():
    path = Path("scripts/update-runtime-env.py").resolve()
    spec = importlib.util.spec_from_file_location("update_runtime_env", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_request_is_atomic_and_single_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    queued = request_update("check")
    assert queued["state"] == "PENDING"
    request_path = tmp_path / "requests" / f"{queued['request_id']}.json"
    assert json.loads(request_path.read_text(encoding="utf-8"))["action"] == "check"
    assert update_status()["pending"] == 1
    with pytest.raises(FileExistsError):
        request_update("update")
    assert not list(tmp_path.rglob("*.tmp"))


def test_restart_request_has_explicit_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    queued = request_update("restart")
    assert queued["action"] == "restart"
    assert queued["phase"] == "RESTARTING"
    assert queued["progress"] == 1


def test_update_agent_restart_uses_privileged_runtime_command(monkeypatch, tmp_path):
    agent = load_update_agent()
    root, state = tmp_path / "repo", tmp_path / "state"
    root.mkdir()
    requests = state / "requests"
    requests.mkdir(parents=True)
    request_path = requests / ("b" * 32 + ".json")
    request_path.write_text(json.dumps({"request_id": "b" * 32, "action": "restart"}),
                            encoding="utf-8")
    calls = []
    monkeypatch.setattr(agent, "run", lambda command, root, extra_env=None:
                        calls.append((command, extra_env)) or "runtime healthy")
    assert agent.process_request(root, state, request_path) is True
    result = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert calls[0][0][-1] == "restart-runtime"
    assert calls[0][1]["VERTEP_UPDATE_STATUS_FILE"].endswith("status.json")
    assert result["state"] == "SUCCEEDED"
    assert result["progress"] == 100
    assert not request_path.exists()


def test_web_update_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "false")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="disabled"):
        request_update("check")


def test_update_agent_reports_failed_operation_to_systemd(monkeypatch, tmp_path):
    agent = load_update_agent()
    import core.update_protocol as protocol
    root, state = tmp_path / "repo", tmp_path / "state"
    root.mkdir()
    request_path = state / "requests" / ("c" * 32 + ".json")
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps({"request_id": "c" * 32, "action": "check"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        protocol, "fetch_manifest", lambda _channel: (_ for _ in ()).throw(
            RuntimeError("update server unavailable")
        )
    )

    assert agent.process_request(root, state, request_path) is False
    status = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED"
    assert status["message"] == "update server unavailable"


def test_update_server_must_be_a_credential_free_https_origin(monkeypatch):
    from core.update_protocol import update_server_url

    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "https://update.vertep.ai")
    assert update_server_url() == "https://update.vertep.ai/"
    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "https://token:secret@example.com")
    with pytest.raises(RuntimeError, match="without credentials"):
        update_server_url()
    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "http://update.vertep.ai")
    with pytest.raises(RuntimeError, match="HTTPS"):
        update_server_url()


def test_github_release_provider_resolves_signed_update_manifest(monkeypatch):
    import core.update_protocol as protocol

    release = {"tag_name": "0.0.0.29", "assets": [{
        "name": "update-manifest-0.0.0.29.json",
        "url": "https://api.github.com/repos/fylypovych/vertep/releases/assets/42",
    }]}
    manifest = {"version": "0.0.0.29", "channel": "stable"}
    responses = [json.dumps(release).encode(), json.dumps(manifest).encode()]
    requests = []

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return self.body

    def urlopen(request, **_):
        requests.append(request)
        return Response(responses.pop(0))

    monkeypatch.setenv(
        "VERTEP_UPDATE_SERVER", "https://api.github.com/repos/fylypovych/vertep/releases")
    monkeypatch.setattr(protocol.urllib.request, "urlopen", urlopen)

    assert protocol.fetch_manifest() == manifest
    assert requests[0].full_url.endswith("/releases/latest")
    assert requests[1].headers["Accept"] == "application/octet-stream"


def test_update_package_github_allowlist_is_repository_scoped(monkeypatch, tmp_path):
    import core.update_protocol as protocol

    monkeypatch.setenv(
        "VERTEP_UPDATE_SERVER", "https://api.github.com/repos/fylypovych/vertep/releases")
    manifest = {
        "package": "https://github.com/attacker/vertep/releases/download/1.0.0/payload.tar.gz",
        "sha256": "0" * 64,
    }
    with pytest.raises(RuntimeError, match="outside"):
        protocol.download_package(manifest, tmp_path / "payload.tar.gz")


def test_manifest_read_retries_after_timeout(monkeypatch):
    import core.update_protocol as protocol

    attempts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _maximum):
            return b'{"version":"0.0.0.67"}'

    def urlopen(_request, **_options):
        attempts.append(True)
        if len(attempts) == 1:
            raise TimeoutError("temporary timeout")
        return Response()

    monkeypatch.setenv("VERTEP_UPDATE_SERVER", "https://updates.example.test")
    monkeypatch.setenv("UPDATE_HTTP_RETRY_DELAY_SECONDS", "0")
    monkeypatch.setattr(protocol.urllib.request, "urlopen", urlopen)

    assert protocol.fetch_manifest()["version"] == "0.0.0.67"
    assert len(attempts) == 2


def test_update_descriptor_is_derived_from_signed_runtime_contract(tmp_path):
    builder = load_update_manifest_builder()
    package = tmp_path / "vertep-runtime-0.0.0.29.tar.gz"
    contract = {
        "version": "0.0.0.29", "channel": "stable", "release_sequence": 29,
        "issued_at": "2026-08-31T00:00:00Z", "expires_at": "2027-08-31T00:00:00Z",
        "compatibility": {"minimum_version": "0.0.0.1", "database_schema": 8,
                          "database_strategy": "expand", "rollback_safe": True},
    }
    encoded = json.dumps(contract).encode()
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo("./manifest.json")
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))

    result = builder.descriptor(package, "0.0.0.29", "fylypovych/vertep")

    assert result["release_sequence"] == 29
    assert result["database"] == {"schema": 8, "strategy": "expand", "rollback_safe": True}
    assert result["package"].endswith(
        "/fylypovych/vertep/releases/download/0.0.0.29/vertep-runtime-0.0.0.29.tar.gz")
    assert len(result["sha256"]) == 64


def test_runtime_update_switches_signed_images_without_rotating_secrets(tmp_path):
    updater = load_runtime_env_updater()
    env = tmp_path / ".env"
    env.write_text(
        "VERTEP_VERSION=0.0.0.27\n"
        "VERTEP_CORE_IMAGE=ghcr.io/fylypovych/vertep-core@sha256:" + "1" * 64 + "\n"
        "POSTGRES_PASSWORD=stable-database-secret\n"
        "LOCAL_OPERATOR_SETTING=preserved\n"
        "GPU_VENDOR=none\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "version": "0.0.0.29",
        "images": {
            "core": {"reference": "ghcr.io/fylypovych/vertep-core:0.0.0.29",
                     "digest": "sha256:" + "2" * 64},
            "comfyui": {"reference": "ghcr.io/fylypovych/vertep-comfyui:0.0.0.29",
                        "digest": "sha256:" + "3" * 64},
        },
    }), encoding="utf-8")

    updater.update(env, manifest, "0.0.0.29")
    result = env.read_text(encoding="utf-8")

    assert "VERTEP_VERSION=0.0.0.29" in result
    assert "VERTEP_CORE_IMAGE=ghcr.io/fylypovych/vertep-core:0.0.0.29@sha256:" + "2" * 64 in result
    assert "POSTGRES_PASSWORD=stable-database-secret" in result
    assert "LOCAL_OPERATOR_SETTING=preserved" in result
    assert "VERTEP_UPDATE_SERVER=https://api.github.com/repos/fylypovych/vertep/releases" in result


def test_update_agent_persists_signed_check_result_and_removes_request(monkeypatch, tmp_path):
    agent = load_update_agent()
    import core.update_protocol as protocol
    root = tmp_path / "repo"
    state = tmp_path / "state"
    root.mkdir()
    requests = state / "requests"
    requests.mkdir(parents=True)
    request_path = requests / ("a" * 32 + ".json")
    request_path.write_text(json.dumps({"request_id": "a" * 32, "action": "check"}), encoding="utf-8")
    monkeypatch.setattr(protocol, "fetch_manifest", lambda channel: {
        "version": "99.0.0", "required": False, "package": "packages/vertep.tar.gz",
        "sha256": "1" * 64, "signature": "signed"})
    monkeypatch.setattr(protocol, "validate_manifest", lambda manifest, public_key, **kwargs: manifest)
    agent.process_request(root, state, request_path)
    assert (state / "system-state.json").is_file()
    result = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert result["state"] == "SUCCEEDED"
    assert result["update_available"] is True
    assert result["available_version"] == "99.0.0"
    assert not request_path.exists()


def test_update_agent_uses_the_host_https_proxy_for_local_readiness(monkeypatch):
    agent = load_update_agent()
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ready":true}'

    def fake_urlopen(request, **options):
        observed.update({"url": request.full_url, **options})
        return Response()

    monkeypatch.delenv("VERTEP_CORE_URL", raising=False)
    monkeypatch.delenv("CORE_URL", raising=False)
    monkeypatch.setattr(agent, "urlopen", fake_urlopen)

    assert agent.core_json("/api/system/update/readiness") == {"ready": True}
    assert observed["url"] == "https://127.0.0.1:8443/api/system/update/readiness"
    assert observed["timeout"] == 10
    assert observed["context"].verify_mode == agent.ssl.CERT_NONE


def test_update_agent_keeps_tls_verification_for_remote_core(monkeypatch):
    agent = load_update_agent()
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ready":true}'

    def fake_urlopen(request, **options):
        observed.update({"url": request.full_url, **options})
        return Response()

    monkeypatch.setenv("VERTEP_CORE_URL", "https://core.example.test")
    monkeypatch.setattr(agent, "urlopen", fake_urlopen)

    assert agent.core_json("/api/system/update/readiness") == {"ready": True}
    assert observed["url"] == "https://core.example.test/api/system/update/readiness"
    assert "context" not in observed


def test_update_api_requires_admin_role(monkeypatch, tmp_path):
    password = "operator-password-long"
    monkeypatch.setenv("ADMIN_PASSWORD", "fallback-admin-password")
    monkeypatch.setenv("USERS_JSON", json.dumps({"operator": {
        "password_hash": _hash_secret(password), "role": "operator"}}))
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    client = TestClient(app)
    assert client.get("/api/system/update", auth=("operator", password)).status_code == 200
    assert client.post("/api/system/update/check", auth=("operator", password)).status_code == 403
    queued = client.post("/api/system/update/check", auth=("admin", "fallback-admin-password"))
    assert queued.status_code == 200
    assert queued.json()["state"] == "PENDING"


def test_update_api_accepts_only_valid_internal_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "fallback-admin-password")
    monkeypatch.setenv("INTERNAL_API_KEY", "host-executor-secret")
    monkeypatch.setenv("WEB_UPDATE_ENABLED", "true")
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    client = TestClient(app)
    assert client.post("/api/system/update/check",
                       headers={"X-Vertep-Internal-Key": "wrong"}).status_code == 401
    queued = client.post("/api/system/update/check",
                         headers={"X-Vertep-Internal-Key": "host-executor-secret"})
    assert queued.status_code == 200
    assert queued.json()["state"] == "PENDING"
