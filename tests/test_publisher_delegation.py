"""Stage 3 of the separately released Publisher (#510/#511): the Worker hands only the upload
to the Publisher module container on its node; without it everything stays as before."""

import base64
import contextlib
import importlib.util
import json
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from publishers.task_secrets import TaskSecrets, task_secrets

ROOT = Path(__file__).resolve().parents[1]


class CoreSecrets(TaskSecrets):
    """TaskSecrets as CORE serves them, without the network; saves are recorded."""

    def __init__(self, data):
        super().__init__("https://core.invalid/api/tasks/t-1/secrets", {})
        self._loaded = data
        self.saved = []

    def save(self, secrets, oauth):
        self.saved.append((secrets, oauth))


class Adapter:
    credential_env = "YOUTUBE_ACCESS_TOKEN"

    def __init__(self, refresh=False, fail=None):
        self.calls, self.refresh, self.fail = [], refresh, fail

    def configured(self):
        return True

    def publish(self, path, metadata):
        from publishers.task_secrets import current_task_secrets
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append((Path(path).parent.name, Path(path).read_bytes(),
                           current_task_secrets().get("youtube_access_token")))
        if self.refresh:
            current_task_secrets().save({"youtube_access_token": "t2"}, {})
        return {"channel": "youtube", "status": "PUBLISHED", "id": "r-1"}


@pytest.fixture
def container(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("publisher_app", ROOT / "modules/publisher/app/publisher_app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    monkeypatch.setattr(app, "TOKEN", "mod-token")
    monkeypatch.setattr(app, "INPUTS", tmp_path / "container-inputs")
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    yield endpoint
    server.shutdown()


def run(task, tmp_path, source, local_adapter):
    from publishers.runner import run_publication
    from worker import role_executor
    return run_publication(
        task,
        artifact=lambda filename, kind, data: {"kind": kind, "data_base64": base64.b64encode(data).decode()},
        core_base_url=lambda: "", fetch_input=lambda *args: None,
        secret_source=lambda item: source,
        use_task_secrets=lambda value: task_secrets(value) if value is not None else contextlib.nullcontext(),
        upload=role_executor._upload_publication)


def receipt(artifacts):
    return json.loads(base64.b64decode(artifacts[0]["data_base64"]))


def setup(monkeypatch, tmp_path, container_adapter, local_adapter, endpoint):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"approved")
    monkeypatch.setenv("JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {"youtube": local_adapter})
    monkeypatch.setattr("worker.application_supervisor.local_module",
                        lambda module_id: (endpoint, "mod-token") if endpoint and module_id == "publisher" else None)
    return video


def test_the_upload_runs_in_the_container_with_task_credentials_and_refreshed_tokens_reach_core(
        container, monkeypatch, tmp_path):
    adapter = Adapter(refresh=True)
    video = setup(monkeypatch, tmp_path, adapter, adapter, container)
    source = CoreSecrets({"secrets": {"youtube_access_token": "t1"}, "oauth": {}})
    result = receipt(run({"job_id": "j-1", "channel": "youtube", "video_path": str(video)}, tmp_path, source, adapter))
    assert result["status"] == "PUBLISHED" and result["id"] == "r-1"
    # Uploaded by the container: from its own input copy, with the task's credentials.
    assert adapter.calls == [("container-inputs", b"approved", "t1")]
    assert source.saved == [({"youtube_access_token": "t2"}, {})]  # refreshed token saved in CORE


def test_without_a_running_container_the_local_adapter_publishes_as_before(monkeypatch, tmp_path):
    local = Adapter()
    video = setup(monkeypatch, tmp_path, None, local, None)
    source = CoreSecrets({"secrets": {"youtube_access_token": "t1"}, "oauth": {}})
    result = receipt(run({"job_id": "j-2", "channel": "youtube", "video_path": str(video)}, tmp_path, source, local))
    assert result["status"] == "PUBLISHED" and local.calls == [(tmp_path.name, b"approved", "t1")]


def test_a_transient_container_error_is_retried_and_a_permanent_one_fails_the_receipt(
        container, monkeypatch, tmp_path):
    from publishers.runner import run_publication  # noqa: F401
    source = CoreSecrets({"secrets": {"youtube_access_token": "t1"}, "oauth": {}})
    for message, transient in (("connection reset by platform", True), ("video rejected by policy", False)):
        failing = Adapter(fail=message)
        video = setup(monkeypatch, tmp_path, failing, Adapter(), container)
        import publishers
        monkeypatch.setattr(publishers, "LIVE_PUBLISHERS", {"youtube": failing})
        task = {"job_id": f"j-{transient}", "channel": "youtube", "video_path": str(video)}
        if transient:
            with pytest.raises(RuntimeError, match="connection"):
                run(task, tmp_path, source, failing)
        else:
            assert receipt(run(task, tmp_path, source, failing))["status"] == "FAILED"


def test_the_supervisor_keeps_the_container_token_private_and_needs_a_ready_container(monkeypatch, tmp_path):
    from worker import application_supervisor as supervisor
    monkeypatch.setenv("VERTEP_APPLICATION_STATE", str(tmp_path / "application-modules.json"))
    monkeypatch.setenv("VERTEP_APPLICATION_MODULES", "true")
    supervisor._save_ports({"publisher": 28100})
    supervisor._save_token("publisher", "secret-token")
    mode = stat.S_IMODE((tmp_path / "application-module-tokens.json").stat().st_mode)
    assert mode == 0o600
    monkeypatch.setattr("httpx.get", lambda *a, **k: type("R", (), {"json": lambda self: {"ready": False}})())
    assert supervisor.local_module("publisher") is None  # not ready: the Worker publishes itself
    monkeypatch.setattr("httpx.get", lambda *a, **k: type("R", (), {"json": lambda self: {"ready": True}})())
    assert supervisor.local_module("publisher") == ("http://127.0.0.1:28100", "secret-token")
    monkeypatch.setenv("VERTEP_APPLICATION_MODULES", "false")
    assert supervisor.local_module("publisher") is None
