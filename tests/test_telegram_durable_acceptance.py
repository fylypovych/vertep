"""Automated acceptance of the durable Telegram confirmation (Issue #253).

The four criteria of #174 are covered by the four test classes below:

* #174.1 — actor/chat/job content callbacks, the duplicate and stale guards
  and the approved-current-version -> target -> Publisher receipt path.
* #174.2 — a crash after the persisted claim and before the effect, the
  replay in a fresh process, the terminal callback and the durable
  actor+target confirmation.
* #174.3 — current -> target/changelog -> confirm -> phases -> final/error,
  the check/apply correlation with the host update agent, the terminal failure
  paths and a retry that is not blocked by a hung operation.
* #174.4 — the restart of a Worker through the shared Fleet executor with
  target/state/drain/effect/health/result and the notification that reaches
  the operator after a process restart.

Only the Telegram transport is controlled (a disposable local Bot API
endpoint).  The adapter, the polling service, the CORE dispatcher, the
durable state, the publisher path, the Fleet control and the host update
agent are the production ones.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "telegram_acceptance_child.py"
sys.path.insert(0, str(REPO_ROOT / "tests"))

from telegram_acceptance_fixtures import (  # noqa: E402
    CHANNEL_ID,
    CHANNEL_TYPE,
    FOREIGN_CHAT,
    OWNER_CHAT,
    PUBLISHER_NODE,
)
from telegram_transport import ControlledTelegramTransport  # noqa: E402

WORKER_TOKEN = "acceptance-worker-token"
INTERNAL_KEY = "acceptance-internal-key"
TARGET_VERSION = "0.0.2.71"
CURRENT_VERSION = "0.0.2.70"
RESTART_NODE = "gpu-1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Acceptance:
    """Disposable CORE environment: controlled transport + durable state."""

    def __init__(self, root: Path, transport: ControlledTelegramTransport) -> None:
        self.root = root
        self.transport = transport
        self.state = root / "state"
        self.jobs_root = self.state / "jobs"
        self.update_state = self.state / "update"
        self.config_root = self.state / "config"
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.env = self._environment()
        self.process: subprocess.Popen | None = None
        self.fixtures: dict = {}
        self.log_path = root / "core.log"
        self._log = None

    # -- environment -------------------------------------------------------
    def _environment(self) -> dict:
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "JOB_ROOT": str(self.jobs_root),
            "UPDATE_STATE_DIR": str(self.update_state),
            "CONFIG_ROOT": str(self.config_root),
            "TELEGRAM_API_BASE": self.transport.base_url,
            "TELEGRAM_BOT_TOKEN": "controlled-token",
            "TELEGRAM_ADMIN_CHAT_IDS": OWNER_CHAT,
            "TELEGRAM_ALLOWED_CHAT_IDS": OWNER_CHAT,
            "TELEGRAM_POLLING_ENABLED": "true",
            "TELEGRAM_POLLING_TIMEOUT": "1",
            "TELEGRAM_POLLING_RETRY_DELAY": "1",
            "TELEGRAM_RETRIES": "1",
            "WEB_UPDATE_ENABLED": "true",
            "SYSTEM_STATE_BACKEND": "file",
            "NODE_API_TOKEN": WORKER_TOKEN,
            "INTERNAL_API_KEY": INTERNAL_KEY,
            "CORE_API_URL": self.base_url,
            "UPDATE_OPERATION_STALE_SECONDS": "2",
            "REQUIRE_WORKER_SELF_TEST": "false",
            "LOCAL_WORKER_FALLBACK": "false",
            "DEMO_MODE": "false",
            "VERTEP_DEMO": "false",
            "MAX_RETRIES": "1",
        })
        return env

    # -- child processes ---------------------------------------------------
    def prepare_config_root(self) -> None:
        """Mark the disposable installation as configured.

        A source installation without ``CONFIG_ROOT`` is always configured; the
        acceptance uses an isolated config root, so it has to carry the
        installation record the API middleware expects.
        """
        self.config_root.mkdir(parents=True, exist_ok=True)
        (self.config_root / "installation.json").write_text(json.dumps({
            "installation_id": "acceptance-installation",
            "installation_name": "Vertep acceptance",
            "version": CURRENT_VERSION,
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:00+00:00",
            "node_role": "core",
            "administrator": {"username": "admin", "role": "admin"},
        }, ensure_ascii=False), encoding="utf-8")

    def run_child(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CHILD), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout,
                              cwd=str(REPO_ROOT))

    def install_fixtures(self) -> dict:
        self.prepare_config_root()
        result = self.run_child("fixtures")
        assert result.returncode == 0, result.stderr
        self.fixtures = json.loads(result.stdout.strip().splitlines()[-1])
        return self.fixtures

    def start_core(self, crash_after_ack: str | None = None) -> subprocess.Popen:
        args = ["serve", str(self.port)]
        if crash_after_ack:
            args += ["--crash-after-ack", crash_after_ack]
        self.log_path = self.root / f"core-{self.port}.log"
        self._log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, str(CHILD), *args], env=self.env,
                                        stdout=self._log, stderr=subprocess.STDOUT,
                                        cwd=str(REPO_ROOT))
        self.wait_ready()
        return self.process

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError(
                    f"CORE process exited early: {self.process.returncode}\n"
                    f"{self.log_path.read_text(encoding='utf-8')[-4000:]}")
            try:
                response = httpx.get(f"{self.base_url}/api/health", timeout=30)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError(
            f"CORE did not become ready\n{self.log_path.read_text(encoding='utf-8')[-4000:]}")

    def stop_core(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)
        self.process = None
        if getattr(self, "_log", None) is not None:
            self._log.close()
            self._log = None

    # -- CORE API ----------------------------------------------------------
    def get(self, path: str, **kwargs) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", timeout=30, **kwargs)

    def post(self, path: str, payload: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.post(f"{self.base_url}{path}", json=payload, timeout=30,
                          headers=headers or {})

    def worker_headers(self) -> dict:
        return {"x-vertep-token": WORKER_TOKEN}

    # -- Telegram ----------------------------------------------------------
    def push_callback(self, callback_id: str, chat_id: str, data: str) -> None:
        self.transport.push_callback_query(callback_id, chat_id, data)

    def wait_answer(self, callback_id: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            answers = self.transport.answers_for(callback_id)
            if answers:
                return str((answers[-1].get("payload") or {}).get("text") or "")
            time.sleep(0.1)
        raise AssertionError(f"No Telegram answer for callback {callback_id}")

    def callback_answer(self, callback_id: str, chat_id: str, data: str,
                        timeout: float = 60.0) -> str:
        self.push_callback(callback_id, chat_id, data)
        return self.wait_answer(callback_id, timeout=timeout)

    # -- durable state -----------------------------------------------------
    def job(self, job_id: str) -> dict:
        path = self.jobs_root / job_id / "job.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def operations(self) -> list[dict]:
        path = self.update_state / "operations.json"
        if not path.exists():
            return []
        return list(json.loads(path.read_text(encoding="utf-8")).values())

    def operation(self, operation_type: str) -> dict | None:
        found = [op for op in self.operations() if op.get("type") == operation_type]
        return found[0] if found else None

    def callback_records(self) -> dict:
        path = self.update_state / "telegram_callbacks.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def pending_notifications(self) -> list[dict]:
        path = self.update_state / "pending_notifications.json"
        if not path.exists():
            return []
        return list(json.loads(path.read_text(encoding="utf-8")).values())

    def host_status(self) -> dict:
        path = self.update_state / "status.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_host_status(self, status: dict) -> None:
        self.update_state.mkdir(parents=True, exist_ok=True)
        (self.update_state / "status.json").write_text(
            json.dumps(status, ensure_ascii=False), encoding="utf-8")

    def host_requests(self) -> list[str]:
        requests = self.update_state / "requests"
        if not requests.is_dir():
            return []
        return sorted(path.stem for path in requests.glob("*.json"))

    def workers(self) -> dict:
        path = self.jobs_root / ".workers.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def acceptance(tmp_path):
    with ControlledTelegramTransport() as transport:
        env = Acceptance(tmp_path, transport)
        env.prepare_config_root()
        try:
            yield env
        finally:
            env.stop_core()


def _release_notes(version: str) -> list[str]:
    path = REPO_ROOT / "releases" / f"{version}.md"
    assert path.is_file(), f"Release notes of {version} are required by the acceptance"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("-")]


class TestContentCallbackAcceptance:
    """#174.1 — content callbacks, guards and the Publisher receipt."""

    def test_foreign_chat_cannot_approve_foreign_job(self, acceptance):
        fixtures = acceptance.install_fixtures()
        acceptance.start_core()
        try:
            text = acceptance.callback_answer("cb-foreign", FOREIGN_CHAT,
                                               f"vid_ok:{fixtures['job_id']}:1")
            assert "не є власником" in text
            assert acceptance.job(fixtures["job_id"])["status"] == "VIDEO_PENDING_APPROVAL"
        finally:
            acceptance.stop_core()

    def test_stale_version_is_rejected(self, acceptance):
        fixtures = acceptance.install_fixtures()
        acceptance.start_core()
        try:
            text = acceptance.callback_answer("cb-stale", OWNER_CHAT,
                                               f"vid_ok:{fixtures['job_id']}:7")
            assert "Stale video approval" in text
            assert acceptance.job(fixtures["job_id"])["status"] == "VIDEO_PENDING_APPROVAL"
        finally:
            acceptance.stop_core()

    def test_current_version_is_approved_once(self, acceptance):
        fixtures = acceptance.install_fixtures()
        acceptance.start_core()
        try:
            text = acceptance.callback_answer("cb-approve", OWNER_CHAT,
                                               f"vid_ok:{fixtures['job_id']}:1")
            assert "схвалено" in text
            job = acceptance.job(fixtures["job_id"])
            assert job["status"] == "READY"
            assert job["video_versions"][0]["approved"] is True
            assert job["video_versions"][0]["approved_by"] == f"telegram:{OWNER_CHAT}"

            duplicate = acceptance.callback_answer("cb-approve-again", OWNER_CHAT,
                                                    f"vid_ok:{fixtures['job_id']}:1")
            assert "Cannot approve video in status READY" in duplicate
            assert acceptance.job(fixtures["job_id"])["status"] == "READY"
        finally:
            acceptance.stop_core()

    def test_publish_reaches_publisher_worker_and_returns_receipt(self, acceptance):
        fixtures = acceptance.install_fixtures()
        acceptance.start_core()
        try:
            acceptance.callback_answer("cb-approve", OWNER_CHAT,
                                       f"vid_ok:{fixtures['job_id']}:1")
            queued = acceptance.callback_answer("cb-publish", OWNER_CHAT,
                                                f"publish_channel:{CHANNEL_ID}:{fixtures['job_id']}")
            assert "чергу" in queued
            job = acceptance.job(fixtures["job_id"])
            assert job["status"] == "PUBLISHING"
            assert len(job["publish_task_ids"]) == 1

            claim = acceptance.post("/api/tasks/claim", {
                "node_name": PUBLISHER_NODE,
                "supported_tasks": ["publish"],
                "supported_workflows": ["*"],
                "capabilities": ["publishing"],
            }, headers=acceptance.worker_headers())
            assert claim.status_code == 200, claim.text
            task = claim.json()["task"]
            assert task and task["task"] == "publish"
            assert task["channel"] == CHANNEL_TYPE

            receipt = {
                "channel": CHANNEL_TYPE,
                "status": "PUBLISHED",
                "remote_id": "controlled-remote-1",
                "url": "https://example.invalid/watch/controlled-remote-1",
                "timestamp": time.time(),
                "video_version": job["active_video_version"],
                "video_sha256": fixtures["video_sha256"],
            }
            artifact = {"kind": "publication_receipt",
                        "data_base64": base64.b64encode(
                            json.dumps(receipt).encode("utf-8")).decode("ascii")}
            result = acceptance.post("/api/tasks/result", {
                "job_id": fixtures["job_id"],
                "node_name": PUBLISHER_NODE,
                "task_id": task["task_id"],
                "success": True,
                "artifacts": [artifact],
            }, headers=acceptance.worker_headers())
            assert result.status_code == 200, result.text

            published = acceptance.job(fixtures["job_id"])
            assert published["status"] == "PUBLISHED"
            assert published["publication_results"][CHANNEL_TYPE]["status"] == "PUBLISHED"
            assert CHANNEL_TYPE in published["published_to"]
        finally:
            acceptance.stop_core()

    def test_duplicate_publish_does_not_enqueue_a_second_task(self, acceptance):
        fixtures = acceptance.install_fixtures()
        acceptance.start_core()
        try:
            acceptance.callback_answer("cb-approve", OWNER_CHAT,
                                       f"vid_ok:{fixtures['job_id']}:1")
            acceptance.callback_answer("cb-publish", OWNER_CHAT,
                                       f"publish_channel:{CHANNEL_ID}:{fixtures['job_id']}")
            job = acceptance.job(fixtures["job_id"])
            task_id = next(iter(job["publish_task_ids"]))
            receipt = {
                "channel": CHANNEL_TYPE,
                "status": "PUBLISHED",
                "remote_id": "controlled-remote-2",
                "url": "https://example.invalid/watch/controlled-remote-2",
                "timestamp": time.time(),
                "video_version": job["active_video_version"],
                "video_sha256": fixtures["video_sha256"],
            }
            artifact = {"kind": "publication_receipt",
                        "data_base64": base64.b64encode(
                            json.dumps(receipt).encode("utf-8")).decode("ascii")}
            claim = acceptance.post("/api/tasks/claim", {
                "node_name": PUBLISHER_NODE,
                "supported_tasks": ["publish"],
                "supported_workflows": ["*"],
                "capabilities": ["publishing"],
            }, headers=acceptance.worker_headers())
            task = claim.json()["task"]
            acceptance.post("/api/tasks/result", {
                "job_id": fixtures["job_id"],
                "node_name": PUBLISHER_NODE,
                "task_id": task["task_id"],
                "success": True,
                "artifacts": [artifact],
            }, headers=acceptance.worker_headers())

            duplicate = acceptance.callback_answer("cb-publish-again", OWNER_CHAT,
                                                    f"publish_channel:{CHANNEL_ID}:{fixtures['job_id']}")
            assert "Опубліковано" in duplicate
            repeated = acceptance.job(fixtures["job_id"])
            # The duplicate callback must not enqueue a second publish task:
            # the completed receipt is the durable marker of the publication.
            assert repeated["publish_task_ids"] == {}
            assert repeated["publication_results"][CHANNEL_TYPE]["remote_id"] == "controlled-remote-2"
        finally:
            acceptance.stop_core()


class TestCrashReplayAcceptance:
    """#174.2 — crash after the durable claim, replay and terminal callback."""

    def _host_status(self, acceptance) -> None:
        acceptance.write_host_status({
            "state": "IDLE", "phase": "NORMAL", "action": None,
            "message": "No update operation has run", "updated_at": None,
            "current_version": CURRENT_VERSION, "available_version": TARGET_VERSION,
            "update_available": True, "request_id": None, "progress": 0, "log": [],
        })

    def test_crash_after_claim_is_replayed_exactly_once(self, acceptance):
        self._host_status(acceptance)
        crashed = acceptance.run_child("crash-after-claim", "cb-crash", OWNER_CHAT,
                                        "sys_update_confirm")
        assert crashed.returncode == 9, crashed.stderr

        record = acceptance.callback_records()["cb-crash"]
        assert record["status"] == "CLAIMED"
        assert record["actor"] == f"telegram:{OWNER_CHAT}"
        assert record["action"] == "sys_update_confirm"
        assert acceptance.operations() == []
        assert acceptance.host_requests() == []

        replayed = acceptance.run_child("callback", "cb-crash", OWNER_CHAT,
                                         "sys_update_confirm")
        assert replayed.returncode == 0, replayed.stderr
        operations = [op for op in acceptance.operations() if op["type"] == "update"]
        assert len(operations) == 1
        assert len(acceptance.host_requests()) == 1

        record = acceptance.callback_records()["cb-crash"]
        assert record["status"] == "COMPLETED"
        # Durable confirmation: actor and confirmed target survive the crash.
        assert record["actor"] == f"telegram:{OWNER_CHAT}"
        assert record["target"] == TARGET_VERSION

        terminal = acceptance.run_child("callback", "cb-crash", OWNER_CHAT,
                                        "sys_update_confirm")
        assert terminal.returncode == 0, terminal.stderr
        answer = acceptance.transport.answers_for("cb-crash")[-1]["payload"]["text"]
        assert "вже оброблено" in answer
        operations = [op for op in acceptance.operations() if op["type"] == "update"]
        assert len(operations) == 1
        assert len(acceptance.host_requests()) == 1

    def test_claimed_callback_keeps_the_original_actor_and_target(self, acceptance):
        self._host_status(acceptance)
        crashed = acceptance.run_child("crash-after-claim", "cb-crash", OWNER_CHAT,
                                        "sys_update_confirm")
        assert crashed.returncode == 9
        record = acceptance.callback_records()["cb-crash"]
        assert record["actor"] == f"telegram:{OWNER_CHAT}"
        assert record["target"] is None

        replayed = acceptance.run_child("callback", "cb-crash", OWNER_CHAT,
                                         "sys_update_confirm")
        assert replayed.returncode == 0, replayed.stderr
        record = acceptance.callback_records()["cb-crash"]
        assert record["actor"] == f"telegram:{OWNER_CHAT}"
        assert record["target"] == TARGET_VERSION
        assert record.get("replayed_at")


class TestUpdateOperationAcceptance:
    """#174.3 — current/target/changelog, phases, failure and retry."""

    def _host_status(self, acceptance, **extra) -> dict:
        status = {
            "state": "IDLE", "phase": "NORMAL", "action": None,
            "message": "No update operation has run", "updated_at": None,
            "current_version": CURRENT_VERSION, "available_version": TARGET_VERSION,
            "update_available": True, "request_id": None, "progress": 0, "log": [],
        }
        status.update(extra)
        acceptance.write_host_status(status)
        return status

    def test_update_prompt_shows_current_target_and_changelog(self, acceptance):
        self._host_status(acceptance)
        acceptance.start_core()
        try:
            acceptance.push_callback("cb-update", OWNER_CHAT, "sys_update")
            deadline = time.monotonic() + 30
            prompt = ""
            while time.monotonic() < deadline:
                messages = acceptance.transport.messages_for(OWNER_CHAT)
                if messages:
                    prompt = str((messages[-1].get("payload") or {}).get("text") or "")
                    if "Поточна версія" in prompt:
                        break
                time.sleep(0.1)
            assert f"Поточна версія: {CURRENT_VERSION}" in prompt
            assert f"Доступна: {TARGET_VERSION}" in prompt
            for line in _release_notes(TARGET_VERSION)[:3]:
                assert line in prompt
        finally:
            acceptance.stop_core()

    def test_confirm_reports_target_and_correlates_the_host_request(self, acceptance):
        self._host_status(acceptance)
        acceptance.start_core()
        try:
            text = acceptance.callback_answer("cb-confirm", OWNER_CHAT, "sys_update_confirm")
            assert f"telegram:{OWNER_CHAT}" in text
            assert TARGET_VERSION in text
            operation = acceptance.operation("update")
            assert operation and operation["status"] in {"QUEUED", "RUNNING"}
            assert operation["external_request_id"] in acceptance.host_requests()
            record = acceptance.callback_records()["cb-confirm"]
            assert record["actor"] == f"telegram:{OWNER_CHAT}"
            assert record["target"] == TARGET_VERSION
        finally:
            acceptance.stop_core()

    def test_host_agent_failure_is_terminal_and_does_not_block_a_retry(self, acceptance):
        self._host_status(acceptance)
        acceptance.start_core()
        try:
            acceptance.callback_answer("cb-confirm", OWNER_CHAT, "sys_update_confirm")
            request_id = acceptance.operation("update")["external_request_id"]
            assert request_id in acceptance.host_requests()

            agent_env = dict(acceptance.env)
            agent_env.update({
                "VERTEP_UPDATE_SERVER": "https://127.0.0.1:1",
                "UPDATE_CHANNEL": "stable",
                "UPDATE_HTTP_RETRIES": "1",
                "UPDATE_HTTP_RETRY_DELAY_SECONDS": "0",
            })
            agent = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "update-agent.py"),
                 "--root", str(REPO_ROOT), "--state-dir", str(acceptance.update_state),
                 "--skip-drain"],
                env=agent_env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT))
            assert agent.returncode == 1, agent.stdout + agent.stderr

            status = acceptance.host_status()
            assert status["state"] == "FAILED"
            assert status["request_id"] == request_id
            assert acceptance.host_requests() == []

            # The CORE correlates the terminal host result with its operation.
            acceptance.push_callback("cb-status", OWNER_CHAT, "sys_status")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                operation = acceptance.operation("update")
                if operation and operation["status"] == "FAILED":
                    break
                time.sleep(0.2)
            operation = acceptance.operation("update")
            assert operation["status"] == "FAILED"
            assert operation["error"]

            # A retry is possible: the failed operation no longer blocks.
            text = acceptance.callback_answer("cb-retry", OWNER_CHAT, "sys_update_confirm")
            assert "Підтверджено" in text
            assert len(acceptance.host_requests()) == 1
        finally:
            acceptance.stop_core()

    def test_hung_operation_is_recovered_and_does_not_block_a_retry(self, acceptance):
        self._host_status(acceptance)
        acceptance.start_core()
        try:
            acceptance.callback_answer("cb-confirm", OWNER_CHAT, "sys_update_confirm")
            operation = acceptance.operation("update")
            request_id = operation["external_request_id"]
            # The host agent took the request and died: no request file, the
            # durable status stays RUNNING and stops progressing.
            for path in (acceptance.update_state / "requests").glob("*.json"):
                path.unlink()
            self._host_status(acceptance, state="RUNNING", phase="CHECKING",
                              request_id=request_id, action="update",
                              updated_at="2020-01-01T00:00:00+00:00",
                              message="Checking signed release manifest")

            text = acceptance.callback_answer("cb-retry", OWNER_CHAT, "sys_update_confirm")
            assert "Підтверджено" in text
            operations = [op for op in acceptance.operations() if op["type"] == "update"]
            assert len(operations) == 2
            assert operations[0]["status"] == "FAILED"
            assert operations[1]["status"] in {"QUEUED", "RUNNING"}
            assert len(acceptance.host_requests()) == 1
        finally:
            acceptance.stop_core()


class TestRestartAcceptance:
    """#174.4 — Fleet restart, durable result and the notification."""

    def _restart_node(self, acceptance) -> None:
        path = acceptance.jobs_root / ".workers.json"
        workers = acceptance.workers()
        workers[RESTART_NODE] = {
            "node_name": RESTART_NODE,
            "role": "worker",
            "status": "READY",
            "supported_tasks": ["image"],
            "supported_workflows": ["*"],
            "capabilities": ["image_generation"],
            "tested_capabilities": [],
            "self_test": {},
            "cancel_fence_ack": {},
            "model_catalog": {},
            "provider_config": {},
            "current_job": None,
            "current_task": None,
            "runtime_instance_id": "instance-before-restart",
            "last_seen": "2026-01-01T00:00:00+00:00",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(workers, ensure_ascii=False), encoding="utf-8")

    def _heartbeat(self, acceptance, instance_id: str) -> httpx.Response:
        return acceptance.post("/api/workers/heartbeat", {
            "node_name": RESTART_NODE,
            "status": "READY",
            "role": "worker",
            "supported_tasks": ["image"],
            "supported_workflows": ["*"],
            "capabilities": ["image_generation"],
            "tested_capabilities": [],
            "self_test": {},
            "cancel_fence_ack": {},
            "model_catalog": {},
            "provider_config": {},
            "runtime_instance_id": instance_id,
        }, headers=acceptance.worker_headers())

    def test_worker_restart_records_target_drain_effect_and_result(self, acceptance):
        acceptance.install_fixtures()
        self._restart_node(acceptance)
        acceptance.start_core()
        try:
            acceptance.push_callback("cb-restart", OWNER_CHAT,
                                     f"sys_restart_worker_confirm:{RESTART_NODE}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                worker = acceptance.workers().get(RESTART_NODE) or {}
                if worker.get("desired_state") == "RESTARTING":
                    break
                time.sleep(0.1)
            worker = acceptance.workers().get(RESTART_NODE) or {}
            assert worker["desired_state"] == "RESTARTING"
            assert worker["status"] == "UPDATING"
            assert worker["restart_operation_id"]

            operation = acceptance.operation("restart")
            assert operation and operation["status"] in {"QUEUED", "RUNNING"}
            assert operation["target"] == RESTART_NODE
            assert operation["current_phase"] == "Drain"

            response = self._heartbeat(acceptance, "instance-after-restart")
            assert response.status_code == 200, response.text
            worker = acceptance.workers().get(RESTART_NODE) or {}
            assert worker["status"] == "READY"
            assert worker["restart_ack"]["operation_id"] == worker["restart_operation_id"]
            assert worker["restart_ack"]["runtime_instance_id"] == "instance-after-restart"

            text = acceptance.wait_answer("cb-restart", timeout=60)
            assert "READY" in text
            operation = acceptance.operation("restart")
            assert operation["status"] == "COMPLETED"
            assert operation["result"]["node_id"] == RESTART_NODE
            assert operation["result"]["runtime_instance_id"] == "instance-after-restart"

            notifications = acceptance.pending_notifications()
            assert any(RESTART_NODE in item["message"] for item in notifications)
        finally:
            acceptance.stop_core()

    def test_restart_effect_survives_a_core_process_restart(self, acceptance):
        acceptance.install_fixtures()
        self._restart_node(acceptance)
        acceptance.start_core(crash_after_ack=RESTART_NODE)
        try:
            acceptance.push_callback("cb-restart", OWNER_CHAT,
                                     f"sys_restart_worker_confirm:{RESTART_NODE}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                worker = acceptance.workers().get(RESTART_NODE) or {}
                if worker.get("desired_state") == "RESTARTING":
                    break
                time.sleep(0.1)
            assert (acceptance.workers().get(RESTART_NODE) or {}).get("restart_operation_id")

            # The node comes back with a new runtime instance and acknowledges
            # the restart; the CORE process dies before it can confirm.
            self._heartbeat(acceptance, "instance-after-restart")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if acceptance.process.poll() is not None:
                    break
                time.sleep(0.05)
            assert acceptance.process.poll() == 9

            operation = acceptance.operation("restart")
            assert operation["status"] in {"QUEUED", "RUNNING"}
        finally:
            acceptance.stop_core()

        # A new CORE process reconciles the surviving effect and delivers the
        # final notification to the operator.
        acceptance.start_core()
        try:
            deadline = time.monotonic() + 30
            operation = None
            while time.monotonic() < deadline:
                operation = acceptance.operation("restart")
                if operation and operation["status"] == "COMPLETED":
                    break
                time.sleep(0.2)
            assert operation["status"] == "COMPLETED"
            assert operation["result"]["runtime_instance_id"] == "instance-after-restart"

            deadline = time.monotonic() + 30
            delivered = ""
            while time.monotonic() < deadline:
                messages = acceptance.transport.messages_for(OWNER_CHAT)
                for message in messages:
                    text = str((message.get("payload") or {}).get("text") or "")
                    if RESTART_NODE in text:
                        delivered = text
                if delivered:
                    break
                time.sleep(0.2)
            assert delivered, "The restart result was not delivered to the operator"
        finally:
            acceptance.stop_core()
