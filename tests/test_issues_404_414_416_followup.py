"""i.0.0.4.4 worker loop, i.0.0.4.14 readiness, i.0.0.4.15 compatibility, i.0.0.4.16 sealed copies."""
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from core.api import tasks
from core.version import WORKER_API
from worker import service


class _Stop(BaseException):
    """Ends the endless worker loop; `except Exception` in the loop must not swallow it."""


def _answer(url, status=200, payload=None):
    return httpx.Response(status, json=payload if payload is not None else {}, request=httpx.Request("POST", url))


class FakeCore:
    def __init__(self, renew_status, result_status, started):
        self.calls = []
        self.renew_status, self.result_status, self.started = renew_status, result_status, started
        self.claims = 0

    def __call__(self, **options):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def post(self, url, json=None, **_):
        path = url.split("http://core", 1)[1]
        self.calls.append(path)
        if path == "/api/tasks/claim":
            self.claims += 1
            if self.claims == 1:
                return _answer(url, payload={"task": {
                    "job_id": "j", "task_id": "t", "task": "image", "node_name": "n"}})
            return _answer(url, payload={"task": None})
        if path == "/api/tasks/renew":
            return _answer(url, self.renew_status)
        if path == "/api/tasks/result":
            return _answer(url, self.result_status)
        return _answer(url, payload={})

    def get(self, url, **_):
        self.calls.append(url.split("http://core", 1)[1])
        return _answer(url, payload=[])


@pytest.mark.parametrize("renew_status,result_status", [(409, 409), (404, 404), (200, 404), (200, 409)])
def test_worker_loop_survives_a_disowned_task(monkeypatch, tmp_path, renew_status, result_status):
    release = threading.Event()
    core = FakeCore(renew_status, result_status, release)
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 2:
            release.set()  # the running attempt ends only after the lease question was asked
        if len(ticks) > 5:
            raise _Stop

    def execute(adapter, task, node):
        release.wait(10)
        return {"job_id": "j", "task_id": "t", "node_name": node, "success": True, "artifacts": []}

    passed = {"status": "PASSED"}
    monkeypatch.setenv("CORE_ADDRESS", "http://core")
    monkeypatch.setenv("NODE_NAME", "n")
    monkeypatch.setenv("NODE_CONFIG_PATH", str(tmp_path / "node.json"))
    monkeypatch.setenv("WORKER_REQUIRE_GPU", "false")
    monkeypatch.setattr(service.httpx, "Client", core)
    monkeypatch.setattr(service.time, "sleep", sleep)
    monkeypatch.setattr(service, "execute_task", execute)
    monkeypatch.setattr(service, "role_self_test", lambda *a: dict(passed))
    monkeypatch.setattr(service, "enroll", lambda *a: "token")
    monkeypatch.setattr(service, "renew_if_needed", lambda *a: "token")
    monkeypatch.setattr(service, "text_model_catalog", lambda: [])
    monkeypatch.setattr(service, "host_metrics", lambda: {})
    monkeypatch.setattr(service.providers, "compute", lambda: SimpleNamespace(cancel=release.set))
    monkeypatch.setattr("worker.gpu_routing.cancel", lambda task_id: None)
    with pytest.raises(_Stop):
        service.main()
    assert core.claims >= 2, "the worker must claim again after the disowned task"
    first_result = core.calls.index("/api/tasks/result")
    assert "/api/workers/heartbeat" in core.calls[first_result:], "heartbeat must continue"
    assert core.calls.count("/api/tasks/result") == 1, "a disowned result is not retried"
    if renew_status != 200:
        assert core.calls.count("/api/tasks/renew") == 1, "a lost lease is not renewed again"


def _node(monkeypatch, responses):
    monkeypatch.setenv("NODE_NAME", "n")
    monkeypatch.setenv("PUBLISHER_MOCK", "false")
    for name in ("YOUTUBE_ACCESS_TOKEN", "FACEBOOK_ACCESS_TOKEN", "TIKTOK_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN",
                 "THREADS_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("worker.role_executor._core_base_url", lambda: "http://core")
    monkeypatch.setattr("worker.role_executor._worker_auth_headers", lambda node: {})
    monkeypatch.setattr(service.httpx, "get", lambda url, **_: httpx.Response(
        200, json=responses, request=httpx.Request("GET", url)))


def test_node_without_channels_is_not_ready(monkeypatch):
    _node(monkeypatch, {"channels": {"youtube": {"ready": False}}})
    with pytest.raises(RuntimeError, match="канал"):
        service.publisher_readiness()


def test_node_reports_the_channels_core_can_serve(monkeypatch):
    _node(monkeypatch, {"channels": {"youtube": {"ready": True}, "tiktok": {"ready": False}}})
    assert service.publisher_readiness() == {"mode": "live", "ready_channels": ["youtube"], "not_ready": {}}


def test_readiness_route_refuses_a_foreign_node_and_has_no_values(monkeypatch):
    request = SimpleNamespace(headers={"x-vertep-node": "n"})
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, req: False)
    with pytest.raises(HTTPException):
        tasks.publisher_readiness(request)
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, req: True)
    monkeypatch.setenv("YOUTUBE_ACCESS_TOKEN", "value-must-not-leak")
    answer = tasks.publisher_readiness(request)
    assert "value-must-not-leak" not in str(answer)
    assert set(answer["channels"]["youtube"]) == {"ready", "configured", "missing_scopes", "reason"}


def test_stub_publication_is_gone():
    source = Path("services/publisher_service.py").read_text(encoding="utf-8")
    assert "PUBLISHED" not in source and "app.post" not in source


def test_incompatible_worker_gets_no_task(monkeypatch):
    from core.models import TaskClaim
    monkeypatch.setattr(tasks.store, "workers", {"n": {"worker_api": WORKER_API + 1}})
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, req: True)
    answer = tasks.claim_task(TaskClaim(node_name="n", vram_mb=0), SimpleNamespace(headers={}))
    assert answer["task"] is None and "контракт" in answer["reason"]


def test_panel_is_told_why_a_worker_gets_no_tasks():
    from core.api.nodes import _node_context
    assert "контракт" in _node_context({"runtime": {"worker_api": WORKER_API + 1}})["compatibility_issue"]
    assert _node_context({"runtime": {"worker_api": WORKER_API}})["compatibility_issue"] is None
    assert _node_context({})["compatibility_issue"] is None


@pytest.mark.parametrize("record", [{}, {"worker_api": None}, {"worker_api": WORKER_API}])
def test_older_or_current_worker_is_not_refused(monkeypatch, record):
    from core.models import TaskClaim
    from core.state import task_queue
    monkeypatch.setattr(tasks.store, "workers", {"n": record})
    monkeypatch.setattr(tasks, "_valid_worker_request", lambda node, req: True)
    monkeypatch.setattr(task_queue, "claim", lambda *a, **k: None)
    answer = tasks.claim_task(TaskClaim(node_name="n", vram_mb=0), SimpleNamespace(headers={}))
    assert "reason" not in (answer or {})


def _bash():
    bash = shutil.which("bash")
    if not bash or not shutil.which("openssl") or os.name == "nt":
        pytest.skip("bash and openssl on a POSIX host are required")
    return bash


def _functions():
    script = Path("scripts/vertep").read_text(encoding="utf-8")
    return script[script.index("update_backup_key(){"):script.index("data_services_ready(){")]


def test_sealed_copy_has_no_readable_secret_and_opens_again(tmp_path):
    bash = _bash()
    root, backup, opened = tmp_path / "root", tmp_path / "root/backups/update-1", tmp_path / "opened"
    backup.mkdir(parents=True)
    opened.mkdir()
    (backup / ".env").write_text("POSTGRES_PASSWORD=very-secret\n")
    script = f'ROOT={root}\n{_functions()}\nseal_update_backup {backup}\nopen_update_backup {backup} {opened}\n'
    subprocess.run([bash, "-ec", script], check=True)
    assert [path.name for path in backup.iterdir()] == ["backup.tar.enc"]
    assert b"very-secret" not in (backup / "backup.tar.enc").read_bytes()
    assert "very-secret" in (opened / ".env").read_text()
    assert (root / ".update-backup.key").stat().st_mode & 0o777 == 0o600


def test_pruning_never_removes_the_current_copy(tmp_path):
    bash = _bash()
    root = tmp_path
    for index in range(5):
        path = root / "backups" / f"update-{index}"
        path.mkdir(parents=True)
        os.utime(path, (index, index))
    current = root / "backups/update-0"
    subprocess.run([bash, "-ec", f"ROOT={root}\n{_functions()}\nprune_update_backups 2 {current}\n"], check=True)
    assert sorted(path.name for path in (root / "backups").iterdir()) == ["update-0", "update-3", "update-4"]


@pytest.mark.parametrize("postgres,redis,core,code", [
    (0, "PONG", 0, 0), (1, "PONG", 0, 1), (0, "NOPE", 0, 1), (0, "PONG", 1, 1)])
def test_update_succeeds_only_with_core_postgres_and_redis(postgres, redis, core, code):
    """A non-zero exit of this stage is what makes the update agent roll back."""
    bash = _bash()
    script = Path("scripts/vertep").read_text(encoding="utf-8")
    ready = script[script.index("data_services_ready(){"):script.index("build_runtime_compose(){")]
    start = script.index('    for attempt in {1..120}; do\n      if [[ "$role" == "worker" ]]')
    stage = script[start:script.index("    ;;", start)].replace("{1..120}", "{1..2}")
    harness = f"""
fake(){{ case "$*" in
  *"ps --services"*) printf 'postgres\\nredis\\n';;
  *pg_isready*) return {postgres};;
  *redis-cli*) echo {redis};;
esac; }}
compose=(fake); curl(){{ return {core}; }}; sleep(){{ :; }}; role=core; core_url=x; core_tls=()
{ready}
{stage}
"""
    assert subprocess.run([bash, "-c", harness], capture_output=True).returncode == code


def test_scripts_contract():
    script = Path("scripts/vertep").read_text(encoding="utf-8")
    assert 'redis-cli -a' not in script
    assert 'seal_update_backup "$backup"' in script and "data_services_ready; then" in script
    assert "--exclude=.update-backup.key" in script
