"""#397 M2: one MPT render lifecycle across separate CORE and Worker *processes*.

CORE runs as ``uvicorn core.app:app``; the node is the stock ``python -m worker.service``
loop. They share nothing but HTTP: CORE dispatches the assembly attempt (it never renders
an external engine), the worker claims/renews it over the queue API, pulls the approved
inputs into its own root, renders through the real ``MoneyPrinterEngine`` against the
pinned runtime and returns a contract CORE validates and imports. Two revisions keep an
immutable history, an approval binds the exact version, a stale approval is refused.

With ``VERTEP_MPT_PINNED_URL``/``VERTEP_MPT_PINNED_TOKEN`` the worker renders on the real
pinned runtime (CI job ``moneyprinter-pinned-runtime``); otherwise on the documented
stand-in that composes the uploaded materials like the pinned pipeline (§9.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from core.artifacts import register_artifact
from core.models import Job, JobStatus, StageStatus, utc_now
from core.pipeline import JobStore
from tests.test_assembly_worker_route import _real_mp4, _wav
from tests.test_mpt_media_matrix import PINNED_TOKEN, PINNED_URL, PinnedRuntime
from tests.test_provider_switch import _ready_report

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin", "mpt-lifecycle-admin")
NODE_TOKEN = "mpt-lifecycle-node-token"
STAND_IN_TOKEN = "mpt-lifecycle-runtime-token"

# The proof belongs to the pinned job (real runtime). The generic suite would only run the
# stand-in, which adds minutes of real processes without being M2 evidence.
pytestmark = pytest.mark.skipif(
    not PINNED_URL and not os.getenv("VERTEP_MPT_PROCESS_LIFECYCLE"),
    reason="M2 process lifecycle runs on the pinned runtime job "
           "(or set VERTEP_MPT_PROCESS_LIFECYCLE=1 for the stand-in)")


# The stock node loop.  Windows lacks os.getloadavg (a POSIX host metric), so only there the
# metric is stubbed before the very same module runs; CI runs it unchanged on Linux.
NODE_COMMAND = ([sys.executable, "-m", "worker.service"] if sys.platform != "win32" else
                [sys.executable, "-c", "import os, runpy; os.getloadavg = lambda: (0.0, 0.0, 0.0); "
                 "runpy.run_module('worker.service', run_name='__main__')"])


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (httpx.HTTPError, OSError, ValueError, KeyError) as error:
            last = error
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}: {last}")


class PinnedProxy:
    """The real pinned runtime behind the controlled-fault handler (#403 R3).

    Submit, status, download and the submit-key routes are answered by the pinned runtime;
    ``submits`` and ``keys`` record the upstream tasks it actually created. A repeated
    submit key is reconciled by the runtime itself, so it adds no entry.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.submits: list[dict] = []
        self.keys: dict[str, str] = {}
        self._tasks: set[str] = set()

    def _answer(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        path = url.split(":8000", 1)[-1]
        self.requests.append((method, path, dict(kwargs)))
        headers = {key: value for key, value in kwargs.get("headers", {}).items()
                   if key not in {"host", "content-length", "accept-encoding", "connection"}}
        response = httpx.request(method, PINNED_URL + path, headers=headers,
                                 content=kwargs.get("content"), timeout=120)
        if method == "POST" and path == "/api/v1/videos" and response.status_code == 200:
            task_id = response.json()["data"]["task_id"]
            if task_id not in self._tasks:
                self._tasks.add(task_id)
                self.submits.append(kwargs.get("json"))
            key = kwargs.get("headers", {}).get("x-vertep-submit-key")
            if key:
                self.keys[key] = task_id
        return response


@pytest.fixture
def runtime_endpoint(tmp_path, request):
    """The pinned runtime the worker renders on: real in CI, documented stand-in locally.

    A test marked ``stand_in`` needs controlled faults (``gate`` holds a submitted task in
    ``processing``, ``faults`` break single answers). With a pinned runtime configured these
    are injected around the real submit/status/download (``pinned-proxy``); without one the
    documented stand-in answers instead. ``route`` records which of them produced a result.
    """
    if PINNED_URL and not request.node.get_closest_marker("stand_in"):
        yield {"url": PINNED_URL, "token": PINNED_TOKEN, "stand_in": None, "gate": None,
               "route": "pinned"}
        return
    stand_in = PinnedProxy() if PINNED_URL else PinnedRuntime(tmp_path)
    gate = threading.Event()
    gate.set()
    faults = {"interrupt_downloads": 0, "hold_submits": 0, "held": 0, "status_errors": [],
              "unreachable_keys": False, "hold_voice": 0, "held_voice": 0,
              "release_held": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def _answer(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            kwargs = {"headers": {key.lower(): value for key, value in self.headers.items()},
                      "content": body}
            if self.headers.get("Content-Type", "").startswith("application/json"):
                kwargs["json"] = json.loads(body or b"{}")
            if self.path == "/health" and not PINNED_URL:  # the node's readiness probe
                response = httpx.Response(200, json=_ready_report())
            elif self.path.startswith("/api/v1/tasks/") and faults["status_errors"]:
                # A queued upstream fault for the next status poll: an HTTP status code,
                # or "malformed" for a 200 whose body has no integer state.
                fault = faults["status_errors"].pop(0)
                response = (httpx.Response(200, json={"data": {"state": "garbled"}})
                            if fault == "malformed" else
                            httpx.Response(fault, json={"status": fault, "message": "fault"}))
            elif self.path.startswith("/api/v1/tasks/") and not gate.is_set():
                response = httpx.Response(200, json={"data": {"state": 4, "progress": 50}})
            elif self.path.startswith("/api/v1/download/") and faults["interrupt_downloads"] > 0:
                # The connection drops mid-body: the full Content-Length is announced, a
                # third of the bytes is sent, then the socket closes.
                faults["interrupt_downloads"] -= 1
                full = stand_in._answer(self.command, "http://engine:8000" + self.path, kwargs)
                self.send_response(200)
                self.send_header("content-type", "video/mp4")
                self.send_header("Content-Length", str(len(full.content)))
                self.end_headers()
                self.wfile.write(full.content[: len(full.content) // 3])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            elif self.path.startswith("/api/v1/videos/") and faults["unreachable_keys"]:
                # The runtime cannot be reached for the submit-key routes: the connection
                # closes without any answer.
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            elif (self.command == "POST" and self.path == "/api/v1/voice"
                  and faults["hold_voice"] > 0):
                # The narration upload never reaches the runtime: the node is stopped
                # while it waits, before anything is staged.
                faults["hold_voice"] -= 1
                faults["held_voice"] += 1
                faults["release_held"].wait(300)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            elif (self.command == "POST" and self.path == "/api/v1/videos"
                  and faults["hold_submits"] > 0):
                # The submit never reaches the upstream: the node is stopped while it
                # waits, so no task ID exists for the attempt.
                faults["hold_submits"] -= 1
                faults["held"] += 1
                faults["release_held"].wait(300)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            elif self.path.startswith("/api/v1/videos/"):
                response = (stand_in._answer(self.command, "http://engine:8000" + self.path, kwargs)
                            if PINNED_URL and gate.is_set() else
                            _submit_key_route(self.command, self.path.rsplit("/", 1)[-1]))
            else:
                response = stand_in._answer(self.command, "http://engine:8000" + self.path, kwargs)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "transfer-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = do_POST = do_DELETE = _answer

    def _submit_key_route(method: str, key: str) -> httpx.Response:
        """The wrapper's durable submit-key routes (§5, §9.8): reconcile and abort.

        A task the upstream is still processing (gate closed) refuses the abort with
        409 and reports ``running``; an unknown key is 404 — nothing of it runs.
        """
        task_id = stand_in.keys.get(key)
        if task_id is None:
            return httpx.Response(404, json={"status": 404, "state": "unknown"})
        running = not gate.is_set()
        if method == "DELETE":
            if running:
                return httpx.Response(409, json={"status": 409, "message": "task is busy"})
            return httpx.Response(200, json={"status": 200, "message": "cancelled"})
        return httpx.Response(200, json={"status": 200, "state": "submitted",
                                         "runtime_state": "running" if running else "finished",
                                         "data": {"task_id": task_id}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}",
               "token": PINNED_TOKEN if PINNED_URL else STAND_IN_TOKEN,
               "stand_in": stand_in, "gate": gate, "faults": faults,
               "route": "pinned-proxy" if PINNED_URL else "stand-in"}
    finally:
        server.shutdown()
        server.server_close()


def _seed_job(job_root: Path) -> str:
    """A Job whose approved scene and narration are ready, awaiting a video render.

    Its id is unique: the submit key derives from it, and the pinned runtime is shared by
    the tests and keeps every key, so a fixed id would reconcile to another test's task.
    """
    from core.orchestration import initialize_plan

    store = JobStore(root=str(job_root))
    job = Job(job_id=f"job-mpt-{uuid.uuid4().hex[:12]}", topic="MPT lifecycle", character_id="test_char",
              priority=5, status=JobStatus.VIDEO_REVISION_REQUESTED, created_at=utc_now(),
              source="web", aspect_ratio="16:9", task_type="video",
              script={"title": "Approved", "scenes": [
                  {"prompt": "Shot", "text": "Approved first line", "duration": 3}]})
    job_dir = job_root / job.job_id
    for name in ("references", "images", "video", "audio", "subtitles", "final", "frames",
                 "storyboard"):
        (job_dir / name).mkdir(parents=True, exist_ok=True)
    clip = _real_mp4(job_dir / "video" / "scene-000.mp4", seconds=3.0)
    _wav(job_dir / "audio" / "voice.wav", seconds=3.0)
    artifact = register_artifact(job, store.root, clip, "video_scene", workflow="ffmpeg")
    initialize_plan(job)
    job.scenes[0].artifact_ids.append(artifact.artifact_id)
    job.scenes[0].status = StageStatus.READY
    store.jobs[job.job_id] = job
    store._save(job)
    return job.job_id


def _engine_env(endpoint: dict) -> dict[str, str]:
    from adapters.providers import MoneyPrinterEngine

    return {"VERTEP_VIDEO_ENGINE": "money-printer",
            MoneyPrinterEngine.env_url: endpoint["url"],
            MoneyPrinterEngine.env_token: endpoint["token"]}


@pytest.fixture
def lifecycle(tmp_path, runtime_endpoint, request):
    engine = getattr(request, "param", "money-printer")
    core_jobs, node_root = tmp_path / "core" / "jobs", tmp_path / "node"
    core_jobs.mkdir(parents=True)
    (tmp_path / "core" / "config").mkdir()
    (tmp_path / "core" / "config" / "installation.json").write_text(
        '{"completed_at": "2026-01-01"}', encoding="utf-8")
    job_id = _seed_job(core_jobs)
    port = _port()
    base = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
            **(_engine_env(runtime_endpoint) if engine == "money-printer"
               else {"VERTEP_VIDEO_ENGINE": "native"}), "NODE_API_TOKEN": NODE_TOKEN,
            "DATABASE_URL": "", "REDIS_URL": "", "SYSTEM_STATE_BACKEND": "file",
            "STATE_BACKEND": "file", "LOCAL_WORKER_FALLBACK": "false"}
    core_env = {**base, "JOB_ROOT": str(core_jobs), "CONFIG_ROOT": str(tmp_path / "core" / "config"),
                "UPDATE_STATE_DIR": str(tmp_path / "core" / "update"),
                "ADMIN_USER": ADMIN[0], "ADMIN_PASSWORD": ADMIN[1], "USERS_JSON": "{}",
                "WATCHDOG_INTERVAL": "1", "RATE_LIMIT_PER_MINUTE": "10000",
                "OLLAMA_URL": "http://127.0.0.1:1"}
    lease = request.node.get_closest_marker("lease_seconds")
    if lease:
        core_env["TASK_LEASE_SECONDS"] = str(lease.args[0])
    node_env = {**base, "CORE_ADDRESS": f"http://127.0.0.1:{port}", "NODE_NAME": "mpt-node",
                "NODE_ROLE": "gpu", "SUPPORTED_TASKS": "assembly", "DEMO_MODE": "true",
                "JOB_ROOT": str(node_root / "jobs"),
                "NODE_CONFIG_PATH": str(node_root / "config" / "node-credentials.json"),
                "CONFIG_ROOT": str(node_root / "config")}
    for name in ("CORE_URL", "CORE_API_URL", "VERTEP_CORE_URL"):
        node_env.pop(name, None)
    logs, processes = [], []

    def start(name, command, env):
        log = (tmp_path / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=ADMIN, timeout=30)
    cores: list[subprocess.Popen] = []

    def start_core() -> subprocess.Popen:
        process = start(f"core-{len(cores)}", [sys.executable, "-m", "uvicorn", "core.app:app",
                                                "--host", "127.0.0.1", "--port", str(port)], core_env)
        cores.append(process)
        _wait(lambda: process.poll() is None and api.get("/api/health").status_code < 500, 120,
              "CORE health")
        return process

    def restart_core() -> subprocess.Popen:
        """A real CORE process restart on the same port and the same persisted state."""
        cores[-1].kill()
        cores[-1].wait(timeout=30)
        return start_core()

    core = start_core()
    try:
        yield {"api": api, "job_id": job_id, "core_jobs": core_jobs, "node_root": node_root,
               "start_node": lambda name="mpt-node": start(name, NODE_COMMAND, {
                   **node_env, "NODE_NAME": name, "JOB_ROOT": str(node_root / name / "jobs"),
                   "NODE_CONFIG_PATH": str(node_root / name / "config" / "node-credentials.json"),
                   "CONFIG_ROOT": str(node_root / name / "config")}),
               "core": core, "restart_core": restart_core, "runtime": runtime_endpoint,
               "tmp_path": tmp_path, "engine": engine}
    finally:
        api.close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
        for log in logs:
            log.close()


@pytest.fixture(autouse=True)
def _trace(request, record_property):
    """#402 M5: every process case leaves its trace in the JUnit — the upstream task IDs and
    submit count the runtime saw, and the versions/hashes CORE imported."""
    if "lifecycle" not in request.fixturenames:
        yield
        return
    lifecycle = request.getfixturevalue("lifecycle")
    yield
    stand_in = lifecycle["runtime"]["stand_in"]
    record_property("route", lifecycle["runtime"]["route"])
    if stand_in is not None:
        record_property("upstream_submits", len(stand_in.submits))
        record_property("upstream_task_ids", json.dumps(sorted(set(stand_in.keys.values()))))
    job_file = lifecycle["core_jobs"] / lifecycle["job_id"] / "job.json"
    if job_file.is_file():
        job = json.loads(job_file.read_text(encoding="utf-8"))
        snapshot = job.get("video_engine_snapshot") or {}
        record_property("engine", str(snapshot.get("engine_id")))
        record_property("submit_key", str(snapshot.get("submit_key")))
        record_property("submit_keys", json.dumps(job.get("assembly_submit_keys") or {}))
        record_property("video_versions", json.dumps(
            [[item["version"], item["sha256"], item.get("approved", False)]
             for item in job.get("video_versions", [])]))


def _job(api, job_id) -> dict:
    response = api.get(f"/api/jobs/{job_id}")
    response.raise_for_status()
    return response.json()


def _render(api, job_id, version: int, timeout: float = 600) -> dict:
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    return _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == version
                 and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                 timeout, f"imported video v{version}")


def test_core_dispatches_and_a_separate_worker_process_renders_two_revisions(lifecycle):
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    node = lifecycle["start_node"]()
    _wait(lambda: any(worker.get("node_name") == "mpt-node"
                      for worker in api.get("/api/workers").json()), 120, "node registration")

    first = _render(api, job_id, 1)
    assert node.poll() is None, "the worker process exited"
    events = "\n".join(first.get("events", []))
    assert "mpt-node ASSEMBLY CLAIMED" in events, events
    v1 = first["video_versions"][0]
    core_file = lifecycle["core_jobs"] / job_id / v1["path"]
    assert hashlib.sha256(core_file.read_bytes()).hexdigest() == v1["sha256"]
    snapshot = first["video_engine_snapshot"]
    assert snapshot["engine_id"] == "money-printer" and snapshot["submit_key"]
    # The node rendered in its own root; CORE imported the bytes from the contract.
    assert (lifecycle["node_root"] / "mpt-node" / "jobs" / job_id).is_dir()
    assert not (lifecycle["node_root"] / "mpt-node" / "jobs" / job_id / v1["path"]).exists()

    # Revision loop: the reviewer regenerates the cut (a video revision of the same
    # approved inputs); v1 stays in the history as it was.
    second = _render(api, job_id, 2)
    versions = {item["version"]: item for item in second["video_versions"]}
    assert versions[1]["sha256"] == v1["sha256"], "the history of v1 changed"
    assert hashlib.sha256(core_file.read_bytes()).hexdigest() == v1["sha256"]
    assert second["video_engine_snapshot"]["submit_key"] != snapshot["submit_key"],         "a new revision must be a new upstream attempt"
    v2 = versions[2]
    assert (lifecycle["core_jobs"] / job_id / v2["path"]).is_file()

    # A stale approval (the superseded v1) is refused; the current v2 is approved as reviewed.
    stale = api.post(f"/api/jobs/{job_id}/video/approve",
                     json={"actor": "lifecycle", "expected_video_version": 1,
                           "expected_sha256": v1["sha256"]})
    assert stale.status_code == 409, stale.text
    approved = api.post(f"/api/jobs/{job_id}/video/approve",
                        json={"actor": "lifecycle", "expected_video_version": 2,
                              "expected_sha256": v2["sha256"]})
    assert approved.status_code == 200, approved.text
    final = {item["version"]: item for item in _job(api, job_id)["video_versions"]}
    assert final[2]["approved"] is True and final[1]["approved"] is False
    assert final[1]["sha256"] == v1["sha256"]
    duplicate = api.post(f"/api/jobs/{job_id}/video/approve",
                         json={"actor": "lifecycle", "expected_video_version": 1,
                               "expected_sha256": v1["sha256"]})
    assert duplicate.status_code == 409, duplicate.text
    stand_in = lifecycle["runtime"]["stand_in"]
    if stand_in is not None:
        assert len(stand_in.submits) == 2, "one upstream task per revision"


@pytest.mark.parametrize("lifecycle", ["native"], indirect=True)
def test_native_engine_regression_renders_in_core_without_a_worker(lifecycle):
    """#397 M2: with Native selected the same CORE process assembles the cut itself on
    the control-plane route; no assembly attempt is dispatched and no node is needed."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    job = _render(api, job_id, 1, timeout=300)
    assert not job.get("assembly_task_ids"), job.get("assembly_task_ids")
    v1 = job["video_versions"][0]
    produced = lifecycle["core_jobs"] / job_id / v1["path"]
    assert hashlib.sha256(produced.read_bytes()).hexdigest() == v1["sha256"]
    assert not any("ASSEMBLY CLAIMED" in event for event in job.get("events", []))
    stand_in = lifecycle["runtime"]["stand_in"]
    if stand_in is not None:
        assert stand_in.submits == [], "Native must not reach the external runtime"


@pytest.mark.stand_in
@pytest.mark.lease_seconds(20)
def test_a_node_killed_mid_render_loses_its_lease_and_another_node_finishes_the_attempt(
        lifecycle):
    """#397 M3: the worker process dies while the upstream task is still processing. Its
    lease expires, CORE hands the same attempt to another node, which renders with the
    same submit key: the runtime reconciles it (one upstream task) and CORE imports the
    result of the new owner only."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    first = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the first node's upstream submit")
    first.kill()
    first.wait(timeout=30)

    second = lifecycle["start_node"]("mpt-node-b")
    _wait(lambda: any("mpt-node-b ASSEMBLY CLAIMED" in event
                      for event in _job(api, job_id).get("events", [])), 240,
          "the attempt reassigned to the second node after the lease expired")
    gate.set()
    job = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                300, "the reassigned attempt imported")
    assert second.poll() is None
    assert len(stand_in.submits) == 1, "the reassigned attempt created a second upstream task"
    claims = [event for event in job["events"] if "ASSEMBLY CLAIMED" in event]
    assert any("mpt-node ASSEMBLY CLAIMED" in event for event in claims), claims
    assert claims[-1].startswith("mpt-node-b") or "mpt-node-b ASSEMBLY CLAIMED" in claims[-1], claims
    v1 = job["video_versions"][0]
    produced = lifecycle["core_jobs"] / job_id / v1["path"]
    assert hashlib.sha256(produced.read_bytes()).hexdigest() == v1["sha256"]


@pytest.mark.stand_in
def test_a_core_restart_mid_render_drops_the_attempt_and_fences_the_late_result(lifecycle):
    """#402 M3: CORE is killed and restarted while the node renders (the upstream task is
    still processing). By #122 P6 CORE cannot resume a Worker-owned attempt: the restart
    drops it and replays the Job from its approved inputs. The node's late result for the
    dropped attempt is refused (409), nothing of it is imported, and the node does not
    resubmit upstream — exactly one upstream task exists.

    Replaying the Job through the whole pipeline needs a complete storyboard/approval
    state, which this seeded Job does not carry; that path is outside this check."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")
    dropped = _job(api, job_id)["assembly_task_id"]
    assert dropped

    lifecycle["restart_core"]()
    recovered = _job(api, job_id)
    assert any("RECOVERED AFTER RESTART" in event for event in recovered["events"]), recovered["events"][-5:]
    assert dropped not in (recovered.get("assembly_task_ids") or {})

    gate.set()
    core_log = lifecycle["tmp_path"] / "core-1.log"
    _wait(lambda: '/api/tasks/result HTTP/1.1" 409' in core_log.read_text(encoding="utf-8",
                                                                          errors="replace"),
          300, "the node's late result refused by the restarted CORE")
    job = _job(api, job_id)
    assert node.poll() is None, "the node process exited"
    assert not job.get("video_versions"), "a result of the dropped attempt was imported"
    assert job.get("active_video_version") is None
    assert len(stand_in.submits) == 1, "the restart caused an ambiguous upstream resubmit"


def _events(api, job_id) -> list[str]:
    return _job(api, job_id).get("events", [])


@pytest.mark.stand_in
def test_a_queued_attempt_cancelled_before_any_node_claims_it_is_released(lifecycle):
    """#402 M3: an attempt cancelled while still queued never reaches the upstream; the
    runtime does not know its submit key, which proves the release, and a node started
    afterwards gets nothing to render."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in = lifecycle["runtime"]["stand_in"]
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: _job(api, job_id).get("assembly_task_id"), 60, "the queued attempt")

    cancelled = api.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    events = _events(api, job_id)
    assert any("ASSEMBLY RELEASE" in event and "RELEASED" in event for event in events), events[-6:]

    node = lifecycle["start_node"]("mpt-node")
    _wait(lambda: any(worker.get("node_name") == "mpt-node"
                      for worker in api.get("/api/workers").json()), 120, "node registration")
    time.sleep(20)  # a claim cycle of the node
    job = _job(api, job_id)
    assert node.poll() is None
    assert job["status"] == JobStatus.CANCELLED.value
    assert not any("ASSEMBLY CLAIMED" in event for event in job["events"])
    assert stand_in.submits == [], "a cancelled queued attempt reached the upstream"


@pytest.mark.stand_in
def test_a_running_attempt_cancel_is_held_until_the_runtime_proves_the_release(lifecycle):
    """#402 M3: cancelling while the upstream is still processing cannot be proven: the
    abort answers busy 409 and the runtime reports ``running``, so CORE records the
    release as unconfirmed instead of a false cancel. The node's late result is
    discarded and nothing of the cancelled attempt is imported; one upstream task."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")

    cancelled = api.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    events = _events(api, job_id)
    assert any("ASSEMBLY RELEASE" in event and "UNCONFIRMED" in event for event in events),         events[-6:]
    assert not any("ASSEMBLY RELEASE" in event and " RELEASED" in event for event in events)

    gate.set()  # the upstream finishes; the node completes and reports late
    time.sleep(30)
    job = _job(api, job_id)
    assert node.poll() is None
    assert job["status"] == JobStatus.CANCELLED.value
    assert not job.get("video_versions"), "a cancelled attempt was imported"
    assert len(stand_in.submits) == 1, "the cancel caused an upstream resubmit"


@pytest.mark.stand_in
def test_an_interrupted_download_is_retried_without_a_new_upstream_generation(lifecycle):
    """#402 M3: the first download of the finished render is cut off. The truncated bytes
    are refused, the download is retried for the same upstream task — no second
    generation — and only the complete render is imported."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in = lifecycle["runtime"]["stand_in"]
    lifecycle["runtime"]["faults"]["interrupt_downloads"] = 1
    node = lifecycle["start_node"]("mpt-node")
    job = _render(api, job_id, 1, timeout=600)
    assert node.poll() is None
    assert lifecycle["runtime"]["faults"]["interrupt_downloads"] == 0, "the fault never fired"
    # The retry may happen inside the node's engine (a fresh download of the same
    # render) or as a CORE retry of the same attempt; either way no new generation.
    assert len(stand_in.submits) == 1, "the retry generated a second upstream render"
    downloads = [path for method, path, _ in stand_in.requests
                 if method == "GET" and path.startswith("/api/v1/download/")]
    assert len(downloads) >= 2, f"the interrupted download was not retried: {downloads}"
    v1 = job["video_versions"][0]
    produced = lifecycle["core_jobs"] / job_id / v1["path"]
    assert hashlib.sha256(produced.read_bytes()).hexdigest() == v1["sha256"]
    assert len(job["video_versions"]) == 1


def test_cleanup_keeps_approved_inputs_history_and_the_submit_key_mapping(lifecycle):
    """#402 M4: after an imported render, the attempt staging is released and the real
    maintenance cleanup runs; the approved inputs and the accepted version stay byte for
    byte, and the durable submit key still reconciles to the same upstream task (a
    duplicate key never becomes a second render)."""
    from core.api.job_helpers import ASSEMBLY_STAGING_DIR

    api, job_id = lifecycle["api"], lifecycle["job_id"]
    job_dir = lifecycle["core_jobs"] / job_id
    approved_inputs = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (job_dir / "video" / "scene-000.mp4", job_dir / "audio" / "voice.wav")}
    lifecycle["start_node"]("mpt-node")
    job = _render(api, job_id, 1)
    v1 = job["video_versions"][0]
    submit_key = job["video_engine_snapshot"]["submit_key"]
    assert not (job_dir / ASSEMBLY_STAGING_DIR).exists(), "attempt staging survived the import"

    cleaned = api.post("/api/maintenance/cleanup", params={"dry_run": "false"})
    assert cleaned.status_code == 200, cleaned.text
    assert job_id not in cleaned.json()["jobs"], "cleanup removed an active Job"
    after = _job(api, job_id)
    assert [item["sha256"] for item in after["video_versions"]] == [v1["sha256"]]
    assert hashlib.sha256((job_dir / v1["path"]).read_bytes()).hexdigest() == v1["sha256"]
    for path, digest in approved_inputs.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"{path.name} changed"

    runtime = lifecycle["runtime"]
    reconciled = httpx.get(f"{runtime['url']}/api/v1/videos/{submit_key}",
                           headers={"Authorization": f"Bearer {runtime['token']}"}, timeout=30)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["data"]["task_id"], reconciled.json()
    if runtime["stand_in"] is not None:
        assert len(runtime["stand_in"].submits) == 1


@pytest.mark.stand_in
@pytest.mark.lease_seconds(20)
def test_the_same_node_restarted_after_the_task_id_reconciles_its_attempt_without_a_resubmit(
        lifecycle):
    """#402 M3: the node process is killed after the upstream task ID exists and the same
    node (same name and credentials) is started again. Its old lease expires, CORE hands
    the attempt back, and the restarted node reconciles the submit key instead of
    creating a second upstream task; the result is imported once."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")
    claims_before = sum("mpt-node ASSEMBLY CLAIMED" in event for event in _events(api, job_id))
    node.kill()
    node.wait(timeout=30)

    restarted = lifecycle["start_node"]("mpt-node")
    _wait(lambda: sum("mpt-node ASSEMBLY CLAIMED" in event
                      for event in _events(api, job_id)) > claims_before, 240,
          "the attempt reclaimed by the restarted node after its lease expired")
    gate.set()
    job = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                300, "the reclaimed attempt imported")
    assert restarted.poll() is None
    assert len(stand_in.submits) == 1, "the node restart created a second upstream task"
    assert len(job["video_versions"]) == 1
    v1 = job["video_versions"][0]
    assert hashlib.sha256((lifecycle["core_jobs"] / job_id / v1["path"]).read_bytes()).hexdigest() \
        == v1["sha256"]


@pytest.mark.stand_in
def test_a_duplicate_result_of_an_imported_attempt_is_fenced_without_a_second_version(lifecycle):
    """#402 M3: after CORE imported an attempt, the same result is delivered again (a
    retried or duplicated node response). The fence refuses it before any side effect:
    no second version, the imported bytes and the approval state stay as they were."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")
    task_id = _job(api, job_id)["assembly_task_id"]
    assert task_id
    gate.set()
    first = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                  and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                  300, "the attempt imported")
    imported = first["video_versions"][0]
    duplicate = httpx.post(f"{str(api.base_url).rstrip('/')}/api/tasks/result", timeout=30,
                           headers={"x-vertep-token": NODE_TOKEN},
                           json={"job_id": job_id, "node_name": "mpt-node", "task_id": task_id,
                                 "success": True, "artifacts": []})
    assert duplicate.status_code in {400, 404, 409}, duplicate.text
    after = _job(api, job_id)
    assert len(after["video_versions"]) == 1, "the duplicate result created a second version"
    assert after["video_versions"][0]["sha256"] == imported["sha256"]
    path = lifecycle["core_jobs"] / job_id / imported["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == imported["sha256"]
    assert len(lifecycle["runtime"]["stand_in"].submits) == 1


@pytest.mark.stand_in
@pytest.mark.lease_seconds(20)
def test_the_same_node_restarted_before_the_task_id_submits_once_after_the_restart(lifecycle):
    """#402 M3: the node is killed while its submit is still in flight, before the upstream
    created a task ID. The restarted node (same name) reclaims the attempt after the lease
    expires and submits it once; exactly one upstream task exists and one version is
    imported."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, faults = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["faults"]
    faults["hold_submits"] = 1
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: faults["held"] == 1, 180, "the node's submit held before any task ID")
    claims_before = sum("mpt-node ASSEMBLY CLAIMED" in event for event in _events(api, job_id))
    node.kill()
    node.wait(timeout=30)
    faults["release_held"].set()
    assert stand_in.submits == [], "the held submit reached the upstream"

    restarted = lifecycle["start_node"]("mpt-node")
    _wait(lambda: sum("mpt-node ASSEMBLY CLAIMED" in event
                      for event in _events(api, job_id)) > claims_before, 240,
          "the attempt reclaimed by the restarted node")
    job = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                300, "the reclaimed attempt imported")
    assert restarted.poll() is None
    assert len(stand_in.submits) == 1, "the restart created more than one upstream task"
    assert len(job["video_versions"]) == 1


def _backup_service(tmp_path: Path, jobs: Path) -> tuple[subprocess.Popen, httpx.Client]:
    """A real Backup Service process whose snapshot source is CORE's job storage."""
    import base64

    port = _port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "BACKUP_ROOT": str(tmp_path / "backups"),
           "BACKUP_SOURCES": f"jobs:{jobs}",
           "BACKUP_RESTORE_STATE_DIR": str(tmp_path / "backup-restore-state"),
           "BACKUP_CONFIG_ROOT": str(tmp_path / "backup-config"),
           "BACKUP_ENCRYPTION_KEY": base64.b64encode(b"p4-synthetic-backup-key-32-bytes").decode(),
           "BACKUP_PG_DUMP_CMD": "", "BACKUP_REDIS_DUMP_CMD": "", "BACKUP_CORE_URL": "",
           "CORE_ADDRESS": "", "SYSTEM_STATE_BACKEND": "file",
           "CONFIG_ROOT": str(tmp_path / "backup-config")}
    log = (tmp_path / "backup-service.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.backup_service:app",
                                "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)

    def ready():
        assert process.poll() is None, (tmp_path / "backup-service.log").read_text(encoding="utf-8")
        try:
            return client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    _wait(ready, 60, "the Backup Service")
    return process, client


def _application_reading(api, job_id) -> dict:
    """What the application itself reports: versions, approvals, hashes it verifies over
    its own artifacts, and the submit-key mapping of the engine decision."""
    job = _job(api, job_id)
    verified = api.post(f"/api/jobs/{job_id}/artifacts/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["valid"] is True, verified.json()
    snapshot = job.get("video_engine_snapshot") or {}
    return {
        "versions": [(item["version"], item["sha256"], item["approved"])
                     for item in job["video_versions"]],
        "active": job.get("active_video_version"),
        "artifact_hashes": sorted((item["artifact_id"], item.get("sha256"))
                                  for item in verified.json()["results"]),
        "engine": snapshot.get("engine_id"), "submit_key": snapshot.get("submit_key"),
        "submit_keys": job.get("assembly_submit_keys") or {},
        "receipts": job.get("publication_results") or {},
        "published_channels": sorted(job.get("published_channels") or []),
    }


def _publish_with_a_receipt(api, job_id) -> None:
    """Publish the approved version through a publisher node: CORE dispatches the task
    and accepts the node's correlated publication receipt."""
    from tests.media_harness import sandbox_receipt
    from worker.role_executor import _artifact

    node = httpx.Client(base_url=str(api.base_url), timeout=30,
                        headers={"x-vertep-token": NODE_TOKEN})
    try:
        heartbeat = node.post("/api/workers/heartbeat", json={
            "node_name": "p4-publisher", "role": "publisher", "status": "FREE", "vram_mb": 0,
            "capabilities": ["publishing"], "supported_tasks": ["publish"],
            "self_test": {"status": "PASSED", "role": "publisher", "checked_at": utc_now()}})
        assert heartbeat.status_code == 200, heartbeat.text
        published = api.post(f"/api/jobs/{job_id}/publish", json=["youtube"])
        assert published.status_code == 200, published.text
        task = _wait(lambda: node.post("/api/tasks/claim", json={
            "node_name": "p4-publisher", "vram_mb": 0, "supported_tasks": ["publish"]}
        ).json().get("task"), 60, "the publish task")
        receipt = sandbox_receipt("youtube", task)
        result = node.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": "p4-publisher",
            "success": True, "artifacts": [_artifact("publication.json", "publication_receipt",
                                                     json.dumps(receipt).encode())]})
        assert result.status_code == 200, result.text
    finally:
        node.close()
    _wait(lambda: _job(api, job_id)["status"] == JobStatus.PUBLISHED.value, 60, "PUBLISHED")


@pytest.mark.parametrize("lifecycle", ["native", "money-printer"], indirect=True)
def test_the_application_reads_back_versions_approvals_receipts_hashes_and_mappings_across_recreate_and_restore(
        lifecycle):
    """#401 P4: CORE itself is the reader of an approved render of each engine. After a
    CORE process recreate, and after the Backup Service restores the job storage that was
    deleted through the API, the restarted CORE reports the same versions, approval,
    self-verified artifact hashes and submit-key mapping."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    if lifecycle["engine"] != "native":
        lifecycle["start_node"]()
    job = _render(api, job_id, 1)
    v1 = job["video_versions"][0]
    approved = api.post(f"/api/jobs/{job_id}/video/approve",
                        json={"actor": "lifecycle", "expected_video_version": 1,
                              "expected_sha256": v1["sha256"]})
    assert approved.status_code == 200, approved.text
    _publish_with_a_receipt(api, job_id)
    original = _application_reading(api, job_id)
    assert original["versions"] == [(1, v1["sha256"], True)]
    assert original["receipts"]["youtube"] and original["published_channels"] == ["youtube"]
    if lifecycle["engine"] != "native":
        assert original["engine"] == "money-printer" and original["submit_key"]

    lifecycle["restart_core"]()
    assert _application_reading(api, job_id) == original, "changed after a CORE recreate"

    tmp_path = lifecycle["tmp_path"]
    backup, service = _backup_service(tmp_path, lifecycle["core_jobs"])
    try:
        created = service.post("/snapshots", json={"job_id": job_id})
        assert created.status_code == 200, created.text
        sid = created.json()["snapshot_id"]
        assert api.delete(f"/api/jobs/{job_id}").status_code == 200
        assert api.get(f"/api/jobs/{job_id}").status_code == 404
        token = service.post(f"/snapshots/{sid}/restore").json()["confirmation_token"]
        confirmed = service.post(f"/snapshots/{sid}/restore/confirm", params={"token": token})
        assert confirmed.status_code == 200, confirmed.text
    finally:
        service.close()
        backup.terminate()
        backup.wait(timeout=20)
    lifecycle["restart_core"]()
    assert _application_reading(api, job_id) == original, "changed after a Backup Service restore"


@pytest.mark.stand_in
def test_transient_429_and_5xx_status_polls_are_bounded_retries_on_the_process_route(lifecycle):
    """#402 M3: the upstream answers the node's status polls with 429/503 a bounded
    number of times. The node keeps polling the same task (no new upstream generation)
    and the render is imported once the runtime answers again."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, faults = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["faults"]
    faults["status_errors"].extend([429, 503, 502, 429])
    lifecycle["start_node"]()
    job = _render(api, job_id, 1)
    assert faults["status_errors"] == [], "the node never polled through the faults"
    assert len(stand_in.submits) == 1, "a transient fault caused a new upstream task"
    assert len(job["video_versions"]) == 1


@pytest.mark.stand_in
def test_persistent_5xx_and_a_malformed_status_fail_the_attempt_and_the_retry_reconciles(
        lifecycle):
    """#402 M3: polls failing past the bound (6 consecutive 503) end the attempt instead of
    holding it forever; a malformed status body is refused as a contract error. CORE
    retries the assembly, the retry reconciles the same submit key (one upstream task)
    and only then is a version imported."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, faults = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["faults"]
    faults["status_errors"].extend([503] * 6 + ["malformed"])
    lifecycle["start_node"]()
    job = _render(api, job_id, 1)
    assert faults["status_errors"] == []
    events = "\n".join(job.get("events", []))
    assert "ASSEMBLY RETRY" in events, events[-2000:]
    assert len(stand_in.submits) == 1, "a retry after the bounded failure resubmitted upstream"
    assert len(job["video_versions"]) == 1


@pytest.mark.stand_in
def test_a_cancel_while_the_runtime_is_unreachable_is_held_unconfirmed(lifecycle):
    """#402 M3: the running attempt is cancelled while the runtime's submit-key routes are
    unreachable. Nothing proves the release, so CORE records it UNCONFIRMED (never a false
    RELEASED); the late result is not imported and there is one upstream task."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    faults = lifecycle["runtime"]["faults"]
    gate.clear()
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")
    faults["unreachable_keys"] = True

    cancelled = api.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    events = _events(api, job_id)
    assert any("ASSEMBLY RELEASE" in event and "UNCONFIRMED" in event for event in events),         events[-6:]
    assert not any("ASSEMBLY RELEASE" in event and " RELEASED" in event for event in events)

    faults["unreachable_keys"] = False
    gate.set()
    time.sleep(30)
    job = _job(api, job_id)
    assert node.poll() is None
    assert job["status"] == JobStatus.CANCELLED.value
    assert not job.get("video_versions"), "a cancelled attempt was imported"
    assert len(stand_in.submits) == 1


@pytest.mark.stand_in
@pytest.mark.lease_seconds(20)
def test_the_same_node_restarted_before_the_voice_is_staged_stages_and_submits_once(lifecycle):
    """#402 M3: the node is killed while its narration upload is in flight, before anything
    is staged or submitted. The restarted node reclaims the attempt, stages the narration
    and submits once; one upstream task and one imported version."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, faults = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["faults"]
    faults["hold_voice"] = 1
    node = lifecycle["start_node"]("mpt-node")
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: faults["held_voice"] == 1, 180, "the node's narration upload held")
    claims_before = sum("mpt-node ASSEMBLY CLAIMED" in event for event in _events(api, job_id))
    node.kill()
    node.wait(timeout=30)
    faults["release_held"].set()
    assert stand_in.submits == [], "a submit happened before the voice was staged"

    restarted = lifecycle["start_node"]("mpt-node")
    _wait(lambda: sum("mpt-node ASSEMBLY CLAIMED" in event
                      for event in _events(api, job_id)) > claims_before, 240,
          "the attempt reclaimed by the restarted node")
    job = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                300, "the reclaimed attempt imported")
    assert restarted.poll() is None
    assert len(stand_in.submits) == 1
    assert len(job["video_versions"]) == 1


@pytest.mark.stand_in
def test_a_render_that_outlives_the_bridge_timeout_fails_the_attempt_and_the_retry_reconciles(
        lifecycle):
    """#402 M3: the upstream keeps the task processing past the bridge's bounded render
    timeout (600 s). The node gives up instead of waiting forever, CORE retries the
    assembly, and the retry reconciles the same submit key: one upstream task, and the
    version is imported only once the runtime finishes."""
    api, job_id = lifecycle["api"], lifecycle["job_id"]
    stand_in, gate = lifecycle["runtime"]["stand_in"], lifecycle["runtime"]["gate"]
    gate.clear()
    lifecycle["start_node"]()
    regenerate = api.post(f"/api/jobs/{job_id}/video/regenerate", json={"actor": "lifecycle"})
    assert regenerate.status_code == 200, regenerate.text
    _wait(lambda: len(stand_in.submits) == 1, 180, "the node's upstream submit")
    started = time.monotonic()
    _wait(lambda: any("ASSEMBLY RETRY" in event for event in _events(api, job_id)), 900,
          "the attempt failed by the bounded render timeout")
    assert time.monotonic() - started >= 550, "the attempt ended before the render timeout"
    assert not _job(api, job_id).get("video_versions")

    gate.set()
    job = _wait(lambda: (job := _job(api, job_id)) and job.get("active_video_version") == 1
                and job["status"] == JobStatus.VIDEO_PENDING_APPROVAL.value and job,
                300, "the retried attempt imported")
    assert len(stand_in.submits) == 1, "the retry after the timeout resubmitted upstream"
    assert len(job["video_versions"]) == 1
