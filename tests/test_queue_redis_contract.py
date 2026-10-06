"""Real Redis contract tests for the queue backend (Issue i.0.0.0.89).

Uses ``VERTEP_TEST_REDIS_URL`` (CI redis service) when set, otherwise spawns a
local ``redis-server`` on a free port.  Without either, the Redis tests skip
cleanly so the in-process suite stays runnable on developer machines; the
Compose persistence contract tests always run.
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from core.queue import TaskQueue

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ping(url: str) -> bool:
    try:
        import redis
        return bool(redis.Redis.from_url(url, socket_connect_timeout=2).ping())
    except Exception:
        return False


def _spawn_redis(directory: Path, binary: str, appendonly: str = "no") -> tuple:
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--dir", str(directory),
         "--save", "", "--appendonly", appendonly],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    for _ in range(100):
        if proc.poll() is not None:
            break
        if _ping(url):
            return proc, url
        time.sleep(0.05)
    _shutdown(proc)
    pytest.fail(f"spawned redis-server did not become ready on port {port}")


def _shutdown(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _flush(queue: TaskQueue) -> None:
    for key in queue._redis.scan_iter(match="vertep:*"):
        queue._redis.delete(key)
    queue._generation = 0


@pytest.fixture(scope="module")
def redis_url(tmp_path_factory):
    env_url = os.getenv("VERTEP_TEST_REDIS_URL", "").strip()
    if env_url:
        if not _ping(env_url):
            pytest.fail(f"VERTEP_TEST_REDIS_URL is set but unreachable: {env_url}")
        yield env_url
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("no Redis: VERTEP_TEST_REDIS_URL is unset and redis-server is missing")
    directory = tmp_path_factory.mktemp("redis-contract")
    proc, url = _spawn_redis(directory, binary)
    yield url
    _shutdown(proc)


@pytest.fixture
def redis_queue(redis_url, monkeypatch):
    monkeypatch.setenv("REDIS_URL", redis_url)
    queue = TaskQueue()
    assert queue.backend == "redis"
    yield queue
    _flush(queue)


def _require_binary() -> str:
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server binary is required for the persistence contract")
    return binary


def test_enqueue_claim_ack_roundtrip(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "contract", "priority": 5})
    assert queue.depth() == 1
    claimed = queue.claim(lease_seconds=30)
    assert claimed["task_id"] == task["task_id"]
    assert queue.depth() == 0
    assert queue.inflight_depth() == 1
    queue.ack(task["task_id"])
    assert queue.inflight_depth() == 0
    assert not queue.has_task(task["task_id"])


def test_priority_claim_order(redis_queue):
    queue = redis_queue
    low = queue.enqueue({"job_id": "low", "priority": 1})
    high = queue.enqueue({"job_id": "high", "priority": 10})
    assert queue.claim(lease_seconds=30)["task_id"] == high["task_id"]
    assert queue.claim(lease_seconds=30)["task_id"] == low["task_id"]
    assert queue.claim() is None


def test_lease_expiry_requeues(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "leased", "priority": 5})
    assert queue.claim(lease_seconds=1)["task_id"] == task["task_id"]
    assert queue.requeue_expired(now=time.time() + 2) == []
    expired = queue.requeue_expired(now=time.time() + 1)
    assert [item["task_id"] for item in expired] == [task["task_id"]]
    assert queue.inflight_depth() == 0
    assert queue.depth() == 1


def test_renew_keeps_the_lease(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "renewed", "priority": 5})
    queue.claim(lease_seconds=1)
    assert queue.renew(task["task_id"], lease_seconds=60) is True
    assert queue.requeue_expired(now=time.time() + 30) == []
    assert queue.inflight_depth() == 1


def test_discard_removes_ready_task(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "cancelled", "priority": 5})
    queue.discard(task["task_id"])
    assert queue.depth() == 0
    assert not queue.has_task(task["task_id"])


def test_dead_letter_roundtrip(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "failed", "scene_id": "scene-001", "priority": 5})
    queue.dead_letter(task, "GPU error")
    assert queue.depth() == 0
    assert queue.dead_letters()[0]["error"] == "GPU error"
    retried = queue.requeue_dead_letter(task["task_id"])
    assert retried is not None
    assert retried["task_id"] != task["task_id"]
    assert queue.dead_letters() == []
    assert queue.has_task(retried["task_id"])


def test_cancellation_channel_roundtrip(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "cancel-me", "priority": 5})
    queue.request_cancel("gpu-01", task["task_id"])
    rows = queue.pop_cancellations("gpu-01")
    assert [row["task_id"] for row in rows] == [task["task_id"]]
    assert queue.pop_cancellations("gpu-01") == []


def test_generation_increases_on_mutation(redis_queue):
    queue = redis_queue
    before = queue.generation()
    queue.enqueue({"job_id": "gen", "priority": 5})
    assert queue.generation() > before


def test_watchdog_lock_is_exclusive(redis_url, monkeypatch):
    monkeypatch.setenv("REDIS_URL", redis_url)
    first = TaskQueue()
    second = TaskQueue()
    assert first.acquire_watchdog_lock(ttl_seconds=30) is True
    assert second.acquire_watchdog_lock(ttl_seconds=30) is False
    _flush(first)


def test_find_reads_ready_and_inflight(redis_queue):
    queue = redis_queue
    task = queue.enqueue({"job_id": "found", "priority": 5})
    assert queue.find(task["task_id"])["job_id"] == "found"
    queue.claim(lease_seconds=30)
    assert queue.inflight_has(task["task_id"])
    assert queue.find(task["task_id"])["job_id"] == "found"
    queue.ack(task["task_id"])
    assert queue.find(task["task_id"]) is None


def test_cross_clients_share_ready_and_lease(redis_url, monkeypatch):
    monkeypatch.setenv("REDIS_URL", redis_url)
    producer = TaskQueue()
    consumer = TaskQueue()
    assert producer.backend == "redis"
    assert consumer.backend == "redis"
    task = producer.enqueue({"job_id": "cross", "priority": 5})
    claimed = consumer.claim(lease_seconds=1)
    assert claimed["task_id"] == task["task_id"]
    assert producer.inflight_has(task["task_id"])
    expired = producer.requeue_expired(now=time.time() + 2)
    assert [item["task_id"] for item in expired] == [task["task_id"]]
    assert consumer.has_task(task["task_id"])
    _flush(producer)


def test_root_compose_redis_enables_appendonly():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    command = compose["services"]["redis"].get("command")
    assert command is not None, "root compose redis must declare an explicit command"
    flat = " ".join(str(part) for part in command)
    assert "appendonly yes" in flat or "--appendonly yes" in flat


def test_deploy_compose_redis_enables_appendonly():
    import yaml

    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    command = compose["services"]["redis"].get("command")
    assert command is not None, "deploy compose redis must declare an explicit command"
    flat = " ".join(str(part) for part in command)
    assert "appendonly yes" in flat


def test_aof_survives_redis_restart(tmp_path, monkeypatch):
    binary = _require_binary()
    directory = tmp_path / "aof"
    directory.mkdir()
    proc, url = _spawn_redis(directory, binary, appendonly="yes")
    monkeypatch.setenv("REDIS_URL", url)
    queue = TaskQueue()
    assert queue.backend == "redis"
    task = queue.enqueue({"job_id": "aof-survives", "priority": 5})
    _shutdown(proc)

    proc2, url2 = _spawn_redis(directory, binary, appendonly="yes")
    monkeypatch.setenv("REDIS_URL", url2)
    restarted = TaskQueue()
    assert restarted.backend == "redis"
    try:
        assert restarted.depth() == 1
        assert restarted.ready_tasks()[0]["task_id"] == task["task_id"]
    finally:
        _flush(restarted)
        _shutdown(proc2)


def test_rdb_snapshot_survives_redis_restart(tmp_path, monkeypatch):
    binary = _require_binary()
    directory = tmp_path / "rdb"
    directory.mkdir()
    proc, url = _spawn_redis(directory, binary)
    monkeypatch.setenv("REDIS_URL", url)
    queue = TaskQueue()
    assert queue.backend == "redis"
    task = queue.enqueue({"job_id": "rdb-survives", "priority": 5})
    queue._redis.save()
    _shutdown(proc)

    proc2, url2 = _spawn_redis(directory, binary)
    monkeypatch.setenv("REDIS_URL", url2)
    restarted = TaskQueue()
    assert restarted.backend == "redis"
    try:
        assert restarted.depth() == 1
        assert restarted.ready_tasks()[0]["task_id"] == task["task_id"]
    finally:
        _flush(restarted)
        _shutdown(proc2)


def test_wiped_data_directory_starts_empty(tmp_path, monkeypatch):
    binary = _require_binary()
    directory = tmp_path / "wiped"
    directory.mkdir()
    proc, url = _spawn_redis(directory, binary, appendonly="yes")
    monkeypatch.setenv("REDIS_URL", url)
    queue = TaskQueue()
    queue.enqueue({"job_id": "wiped", "priority": 5})
    _shutdown(proc)
    for path in directory.iterdir():
        path.unlink()

    proc2, url2 = _spawn_redis(directory, binary, appendonly="yes")
    monkeypatch.setenv("REDIS_URL", url2)
    restarted = TaskQueue()
    assert restarted.backend == "redis"
    try:
        assert restarted.depth() == 0
    finally:
        _flush(restarted)
        _shutdown(proc2)
