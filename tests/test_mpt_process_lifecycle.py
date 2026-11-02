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


@pytest.fixture
def runtime_endpoint(tmp_path):
    """The pinned runtime the worker renders on: real in CI, documented stand-in locally."""
    if PINNED_URL:
        yield {"url": PINNED_URL, "token": PINNED_TOKEN, "stand_in": None}
        return
    stand_in = PinnedRuntime(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return None

        def _answer(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            kwargs = {"headers": {key.lower(): value for key, value in self.headers.items()},
                      "content": body}
            if self.headers.get("Content-Type", "").startswith("application/json"):
                kwargs["json"] = json.loads(body or b"{}")
            if self.path == "/health":  # the node's readiness probe of the wrapper
                response = httpx.Response(200, json=_ready_report())
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "token": STAND_IN_TOKEN,
               "stand_in": stand_in}
    finally:
        server.shutdown()
        server.server_close()


def _seed_job(job_root: Path) -> str:
    """A Job whose approved scene and narration are ready, awaiting a video render."""
    from core.orchestration import initialize_plan

    store = JobStore(root=str(job_root))
    job = Job(job_id="job-mpt-lifecycle", topic="MPT lifecycle", character_id="test_char",
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

    core = start("core", [sys.executable, "-m", "uvicorn", "core.app:app", "--host", "127.0.0.1",
                          "--port", str(port)], core_env)
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=ADMIN, timeout=30)
    _wait(lambda: core.poll() is None and api.get("/api/health").status_code < 500, 120,
          "CORE health")
    try:
        yield {"api": api, "job_id": job_id, "core_jobs": core_jobs, "node_root": node_root,
               "start_node": lambda: start("node", NODE_COMMAND, node_env),
               "core": core, "runtime": runtime_endpoint, "tmp_path": tmp_path}
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
    assert (lifecycle["node_root"] / "jobs" / job_id).is_dir()
    assert not (lifecycle["node_root"] / "jobs" / job_id / v1["path"]).exists()

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
