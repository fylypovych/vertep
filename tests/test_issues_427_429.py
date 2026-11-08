"""i.0.0.4.27 update copies and job block, i.0.0.4.28 download, i.0.0.4.29 Publisher readiness."""
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from core.system_state import SystemState, jobs_may_be_created, set_system_state
from publishers.base import Publisher
from publishers.facebook import FacebookPublisher
from publishers.telegram import TelegramPublisher
from publishers.transport import FakeTransport
from publishers.youtube import YoutubePublisher
from services import backup_service

SECRET = "value-must-not-leak"


# ---- #427: job creation during an update
@pytest.mark.parametrize("state,operation,allowed", [
    (SystemState.MAINTENANCE, "a" * 32, False),
    (SystemState.MAINTENANCE, "restore:snap", True),
    (SystemState.MAINTENANCE, None, True),
    (SystemState.UPDATING, "a" * 32, False),
    (SystemState.NORMAL, None, True)])
def test_no_job_is_created_while_an_update_runs(tmp_path, monkeypatch, state, operation, allowed):
    monkeypatch.setenv("UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SYSTEM_STATE_BACKEND", "file")
    set_system_state(state, "test", operation, tmp_path)
    assert jobs_may_be_created() is allowed


def test_api_and_telegram_share_the_block_and_the_panel_mirrors_it():
    assert "jobs_may_be_created()" in Path("core/api/jobs.py").read_text(encoding="utf-8")
    assert "jobs_may_be_created()" in Path("core/telegram/handlers.py").read_text(encoding="utf-8")
    policy = Path("web-v2/src/app/core/services/policy.service.ts").read_text(encoding="utf-8")
    assert "!operation.startsWith('restore:')" in policy


# ---- #427: the copy before an update
def _bash():
    bash = shutil.which("bash")
    if not bash or not shutil.which("openssl") or not shutil.which("sha256sum") or os.name == "nt":
        pytest.skip("bash, openssl and sha256sum on a POSIX host are required")
    return bash


def _script():
    return Path("scripts/vertep").read_text(encoding="utf-8")


def _functions():
    script = _script()
    return script[script.index("update_backup_key(){"):script.index("data_services_ready(){")]


def _backup_stage(root: Path, archive_command: str) -> str:
    script = _script()
    stage = script[script.index('    backup_final="$ROOT/backups/update-$stamp"'):script.index('    update_progress 58')]
    return (f'set -e\nROOT={root}\nrole=worker\nstamp=1\ntarget_version=9\nmkdir -p "$ROOT/backups"\n'
            f'archive_live(){{ {archive_command}; }}\n{_functions()}\n{stage}\n')


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text(f"POSTGRES_PASSWORD={SECRET}\n")
    (root / ".vertep-last-good").write_text(str(root / "backups/update-0"))
    return root


def test_interrupted_copy_leaves_nothing_readable_and_no_rollback_target(installation):
    bash = _bash()
    # The failure comes after the secrets could have been copied, as a stopped pg_dump would.
    stage = _backup_stage(installation, 'cp "$ROOT/.env" "$backup/.env"; false')
    assert subprocess.run([bash, "-c", stage]).returncode != 0
    assert not (installation / ".vertep-last-good").exists(), "an older copy would lose this update's data"
    assert not list((installation / "backups").iterdir()), "an unsealed copy survived the failure"


def test_killed_copy_is_private_and_swept_by_the_next_update(installation):
    bash = _bash()
    stage = _backup_stage(installation, 'cp "$ROOT/.env" "$backup/.env"; kill -9 $$')
    subprocess.run([bash, "-c", stage])
    work = list((installation / "backups").glob(".work-*"))
    assert work and work[0].stat().st_mode & 0o777 == 0o700
    finished = _backup_stage(installation, 'printf x > "$backup/application.tar.gz"')
    subprocess.run([bash, "-ec", finished], check=True)
    assert [path.name for path in (installation / "backups").iterdir()] == ["update-1"]


def _rollback_check() -> str:
    script = _script()
    return script[script.index('    backup=$(cat "$ROOT/.vertep-last-good")'):script.index('    python3 "$ROOT/scripts/release-layout.py" rollback "$ROOT"')]


def _finished_copy(bash, root):
    stage = _backup_stage(root, 'printf application > "$backup/application.tar.gz"')
    subprocess.run([bash, "-ec", stage], check=True)
    return root / "backups/update-1"


def _rollback_reads(bash, root):
    return subprocess.run([bash, "-c", f"set -e\nROOT={root}\n{_functions()}\n{_rollback_check()}\n"],
                          capture_output=True, text=True)


def test_rollback_opens_a_sealed_copy_and_refuses_a_damaged_one(installation):
    bash = _bash()
    copy = _finished_copy(bash, installation)
    assert [path.name for path in copy.iterdir()] == ["backup.tar.enc"]
    assert SECRET.encode() not in (copy / "backup.tar.enc").read_bytes()
    assert (installation / ".vertep-last-good").read_text().strip() == str(copy)
    assert _rollback_reads(bash, installation).returncode == 0
    data = bytearray((copy / "backup.tar.enc").read_bytes())
    data[len(data) // 2] ^= 0xFF
    (copy / "backup.tar.enc").write_bytes(bytes(data))
    assert _rollback_reads(bash, installation).returncode != 0


def test_rollback_still_reads_the_old_unsealed_format(installation):
    bash = _bash()
    copy = installation / "backups/update-old"
    copy.mkdir(parents=True)
    (copy / "application.tar.gz").write_text("application")
    subprocess.run([bash, "-c", f'cd {copy} && sha256sum application.tar.gz > SHA256SUMS'], check=True)
    (installation / ".vertep-last-good").write_text(str(copy))
    assert _rollback_reads(bash, installation).returncode == 0
    (copy / "application.tar.gz").write_text("damaged")
    assert _rollback_reads(bash, installation).returncode != 0


def test_retention_keeps_the_newest_and_the_copy_rollback_needs(installation):
    bash = _bash()
    backups = installation / "backups"
    for index in range(5):
        path = backups / f"update-{index}"
        path.mkdir(parents=True)
        os.utime(path, (index + 1, index + 1))
    current = backups / "update-0"
    subprocess.run([bash, "-ec", f"ROOT={installation}\n{_functions()}\nprune_update_backups 2 {current}\n"], check=True)
    assert sorted(path.name for path in backups.iterdir()) == ["update-0", "update-3", "update-4"]


# ---- #428: a full multi-part download through both hops
@pytest.fixture
def backup_host(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_service, "_backup_root", lambda: tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(backup_service.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    monkeypatch.setenv("BACKUP_URL", f"http://127.0.0.1:{port}")
    yield tmp_path
    server.should_exit = True
    thread.join(timeout=10)


def test_download_over_512_mb_arrives_whole_through_core(backup_host):
    from core.api.backups import download_system_backup
    expected = hashlib.sha256()
    part = 300 * 1024 * 1024
    for index, name in enumerate(("snap.vtbackup", "snap.vtbackup.part0001")):
        with (backup_host / name).open("wb") as target:
            target.write(bytes([index + 1]) * 1024)
            target.truncate(part)
        expected.update(bytes([index + 1]) * 1024)
        expected.update(b"\0" * (part - 1024))
    (backup_host / "backup.key").write_bytes(b"secret-key")
    response = download_system_backup("snap")
    assert int(response.headers["content-length"]) == part * 2
    digest, size, largest = hashlib.sha256(), 0, 0

    async def read():
        nonlocal size, largest
        async for chunk in response.body_iterator:
            digest.update(chunk)
            size += len(chunk)
            largest = max(largest, len(chunk))

    asyncio.run(read())
    assert size == part * 2 and digest.hexdigest() == expected.hexdigest()
    assert largest <= 1024 * 1024 * 2, "the file must be streamed, not buffered"


def _session(monkeypatch, tmp_path, role):
    import hashlib as hashes
    import hmac
    from core.security import _session_token
    password = "download-role-password"
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    monkeypatch.setenv("ADMIN_USER", "primary-admin")
    monkeypatch.setenv("CONFIG_ROOT", str(tmp_path))
    (tmp_path / "installation.json").write_text(
        json.dumps({"installation_id": "test", "completed_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setenv("USERS_JSON", json.dumps({"reader": {"password_hash": "x", "role": "viewer"},
                                                 "primary-admin": {"password_hash": "x", "role": "admin"}}))
    user = "reader" if role == "viewer" else "primary-admin"
    token = _session_token(user, role)
    return {"vertep_session": token, "vertep_csrf": hmac.new(password.encode(), token.encode(), hashes.sha256).hexdigest()}


@pytest.mark.parametrize("role,status", [(None, 401), ("viewer", 403)])
def test_only_an_administrator_gets_the_file(monkeypatch, tmp_path, role, status):
    from core.app import app
    cookies = _session(monkeypatch, tmp_path, role) if role else {}
    if not role:
        _session(monkeypatch, tmp_path, "admin")
    client = TestClient(app, cookies=cookies)
    client.headers.pop("Authorization", None)
    assert client.get("/api/system/backups/snap/download").status_code == status


@pytest.mark.parametrize("snapshot", ["..%2F..%2Fbackup", "a%20b", "snap.key"])
def test_path_substitution_is_refused(backup_host, snapshot):
    client = TestClient(backup_service.app)
    assert client.get(f"/snapshots/{snapshot}/download").status_code in {400, 404}


# ---- #429: readiness asks the adapter, publishes nothing, returns no secret
def _clean(monkeypatch):
    monkeypatch.delenv("VERTEP_PUBLISHER_MOCK", raising=False)
    monkeypatch.setattr("publishers.base.publisher_mock_enabled", lambda: False)
    monkeypatch.setattr("publishers.oauth._get_secret", lambda name: os.getenv(name) or "")


def _only_read(transport):
    assert all(method == "GET" for method, _, _ in transport.requests), "readiness must not publish"


def test_a_token_the_platform_refuses_is_not_ready(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)
    transport = FakeTransport([httpx.Response(401)])
    state = YoutubePublisher(transport).readiness()
    assert state["ready"] is False and "відхилила" in state["reason"]
    assert SECRET not in json.dumps(state)
    _only_read(transport)


def test_a_valid_token_is_ready(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)
    transport = FakeTransport([httpx.Response(200, json={"items": [{"id": "c"}]})])
    assert YoutubePublisher(transport).readiness()["ready"] is True
    _only_read(transport)


def test_an_unreachable_platform_does_not_count_as_ready(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)

    def down():
        raise httpx.ConnectError(f"cannot reach ?token={SECRET}")

    state = YoutubePublisher(FakeTransport([down])).readiness()
    assert state["ready"] is False and SECRET not in json.dumps(state)


def test_a_token_that_cannot_refresh_is_expired(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)
    publisher = YoutubePublisher(FakeTransport())

    class Expired:
        def access_token(self):
            raise RuntimeError(f"refresh failed for {SECRET}")

        def refresh_token(self):
            return "refresh"

    monkeypatch.setattr(publisher, "_token_provider", lambda: Expired())
    state = publisher.readiness()
    assert state["ready"] is False and "прострочений" in state["reason"]
    assert SECRET not in json.dumps(state)


def test_missing_identifier_is_named_without_any_request(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("FACEBOOK_ACCESS_TOKEN", SECRET)
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    transport = FakeTransport()
    state = FacebookPublisher(transport).readiness()
    assert state["ready"] is False and "FACEBOOK_PAGE_ID" in state["reason"] and not transport.requests


def test_missing_permissions_are_named(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)
    publisher = YoutubePublisher(FakeTransport())
    monkeypatch.setattr(Publisher, "token_scopes", lambda self: ["other.scope"])
    state = publisher.readiness()
    assert state["ready"] is False and state["missing_scopes"] == publisher.required_scopes


def test_absent_credentials_are_not_ready(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.delenv("YOUTUBE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(YoutubePublisher, "configured", lambda self: False)
    state = YoutubePublisher(FakeTransport()).readiness()
    assert state == {"ready": False, "configured": False, "missing_scopes": [], "reason": "Немає облікових даних"}


def test_telegram_checks_the_bot_without_sending(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    transport = FakeTransport([httpx.Response(401)])
    state = TelegramPublisher(transport).readiness()
    assert state["ready"] is False and SECRET not in json.dumps(state)
    assert transport.requests[0][1].endswith("/getMe")
    _only_read(transport)


def _telegram(responses):
    transport = FakeTransport(responses)
    return TelegramPublisher(transport), transport


GET_ME = httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True}})


def test_telegram_without_a_channel_is_not_ready(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    publisher, transport = _telegram([GET_ME])
    state = publisher.channel_readiness("")
    assert state["ready"] is False and "канал" in state["reason"]
    _only_read(transport)


@pytest.mark.parametrize("answer,ready", [
    (httpx.Response(200, json={"result": {"status": "administrator", "can_post_messages": True}}), True),
    (httpx.Response(200, json={"result": {"status": "creator"}}), True),
    (httpx.Response(200, json={"result": {"status": "administrator", "can_post_messages": False}}), False),
    (httpx.Response(200, json={"result": {"status": "left"}}), False),
    # #440: only a proven right to post counts.
    (httpx.Response(200, json={"result": {"status": "member"}}), False),
    (httpx.Response(200, json={"result": {"status": "restricted"}}), False),
    (httpx.Response(200, json={"result": {"status": "administrator"}}), False),
    (httpx.Response(200, json={"result": {"status": "administrator", "can_post_messages": "true"}}), False),
    (httpx.Response(200, json={"ok": True}), False),
    (httpx.Response(200, json={"result": ["creator"]}), False),
    (httpx.Response(200, content=b"not json"), False),
    (httpx.Response(400, json={"description": "chat not found"}), False),
    (httpx.Response(403, json={"description": "bot was kicked"}), False)])
def test_telegram_channel_permission_decides_readiness(monkeypatch, answer, ready):
    _clean(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    publisher, transport = _telegram([GET_ME, answer])
    state = publisher.channel_readiness("@channel")
    assert state["ready"] is ready and SECRET not in json.dumps(state)
    assert transport.requests[1][1].endswith("/getChatMember")
    _only_read(transport)


def test_a_synthetic_token_on_the_node_does_not_make_it_ready(monkeypatch):
    from publishers import LIVE_PUBLISHERS
    from worker import service
    _clean(monkeypatch)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", SECRET)
    monkeypatch.setattr("worker.role_executor._core_base_url", lambda: "")
    monkeypatch.setattr(LIVE_PUBLISHERS["youtube"], "transport", FakeTransport([httpx.Response(401)]))
    for name, publisher in LIVE_PUBLISHERS.items():
        if name != "youtube":
            monkeypatch.delenv(publisher.credential_env, raising=False)
    with pytest.raises(RuntimeError) as refused:
        service.publisher_readiness()
    assert "youtube" in str(refused.value) and SECRET not in str(refused.value)



class _CoreReadiness:
    """CORE's /api/tasks/publisher-readiness answer for a Publisher Node."""

    def __init__(self, target):
        self.target = target

    def __call__(self, url, **kwargs):
        assert url.endswith("/api/tasks/publisher-readiness")
        return httpx.Response(200, json={
            "channels": {"telegram": {"ready": False, "reason": "CORE не має токена"}},
            "targets": {"telegram": self.target}}, request=httpx.Request("GET", url))


def _node_with_telegram(monkeypatch, responses, target):
    from publishers import LIVE_PUBLISHERS
    from worker import service
    _clean(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("NODE_NAME", "publisher-1")
    for name, publisher in LIVE_PUBLISHERS.items():
        if name != "telegram":
            monkeypatch.delenv(publisher.credential_env, raising=False)
    transport = FakeTransport(responses)
    monkeypatch.setattr(LIVE_PUBLISHERS["telegram"], "transport", transport)
    monkeypatch.setattr("worker.role_executor._core_base_url", lambda: "http://core")
    monkeypatch.setattr("worker.role_executor._worker_auth_headers", lambda node: {})
    monkeypatch.setattr(service.httpx, "get", _CoreReadiness(target))
    return service, transport


@pytest.mark.parametrize("target,member", [
    ("", None),
    ("@channel", {"status": "member"}),
    ("@channel", {"status": "administrator", "can_post_messages": False})])
def test_a_node_token_is_not_ready_without_the_core_channel_or_the_right(monkeypatch, target, member):
    """#440: a node with its own valid token is judged on the channel CORE configured."""
    responses = [GET_ME] + ([httpx.Response(200, json={"result": member})] if member else [])
    service, transport = _node_with_telegram(monkeypatch, responses, target)
    with pytest.raises(RuntimeError) as refused:
        service.publisher_readiness()
    assert "telegram" in str(refused.value) and SECRET not in str(refused.value)
    _only_read(transport)


def test_a_node_token_with_the_right_to_post_is_ready(monkeypatch):
    service, transport = _node_with_telegram(monkeypatch, [GET_ME, httpx.Response(
        200, json={"result": {"status": "administrator", "can_post_messages": True}})], "@channel")
    assert service.publisher_readiness()["ready_channels"] == ["telegram"]
    assert transport.requests[1][1].endswith("/getChatMember")
    _only_read(transport)


def test_core_reports_the_target_it_checks_without_secrets(monkeypatch):
    from core.api import tasks
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, request: True)
    monkeypatch.setattr("core.publishing_settings.publishing_settings", lambda: {"telegram_channel_id": "@channel"})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tasks.router)
    answer = TestClient(app).get("/api/tasks/publisher-readiness", headers={"x-vertep-node": "p"}).json()
    assert answer["targets"] == {"telegram": "@channel"}
    assert SECRET not in json.dumps(answer)

# ---- #426
def test_release_bundle_refuses_a_private_key(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_bundle_426", "scripts/release-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keys = tmp_path / "installer/root-keys"
    keys.mkdir(parents=True)
    (keys / "root-1.pem").write_text("-----BEGIN PUBLIC KEY-----\n")
    assert "installer/root-keys/root-1.pem" in module.collect_manifest(tmp_path)
    (keys / "root-1.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    with pytest.raises(ValueError, match="Private key"):
        module.collect_manifest(tmp_path)


def test_public_root_keys_and_metadata_validate_as_committed():
    from core.update_trust import validate_root_metadata
    metadata = json.loads(Path("installer/root-keys/root-metadata.json").read_text(encoding="utf-8"))
    validated = validate_root_metadata(metadata, Path("installer/root-keys"))
    assert validated
