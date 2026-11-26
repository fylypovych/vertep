"""Stage 1 of the separately released Publisher (#510/#511, owner decision 21.11.2026):
publication runs from ``publishers.runner`` with the node specifics passed in, so the same
code serves the Worker today and the Publisher module container next."""

import base64
import json
import sys

import pytest


def hooks(calls):
    return dict(
        artifact=lambda filename, kind, data: {"filename": filename, "kind": kind,
                                               "data_base64": base64.b64encode(data).decode()},
        core_base_url=lambda: "",
        fetch_input=lambda *args: calls.append(("fetch", args)),
        secret_source=lambda task: None,
        use_task_secrets=lambda source: __import__("contextlib").nullcontext())


def receipt(artifacts):
    [item] = artifacts
    assert item["kind"] == "publication_receipt"
    return json.loads(base64.b64decode(item["data_base64"]))


def test_publication_runs_without_the_worker_process(monkeypatch, tmp_path):
    from publishers.runner import run_publication
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("PUBLISHER_MOCK", "true")
    monkeypatch.setenv("JOB_ROOT", str(tmp_path))
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")
    calls = []
    result = receipt(run_publication({"job_id": "job-1", "channel": "youtube", "video_path": str(video)},
                                     **hooks(calls)))
    assert result["status"] == "PUBLISHED" and result["job_id"] == "job-1" and result["channel"] == "youtube"
    assert calls == []
    import ast
    import publishers.runner as runner
    imported = {node.module or "" for node in ast.walk(ast.parse(open(runner.__file__).read()))
                if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(ast.parse(open(runner.__file__).read()))
                 if isinstance(node, ast.Import) for alias in node.names}
    # No Worker code; from CORE only the secret masking the Publisher image already ships.
    assert not [name for name in imported if name.split(".")[0] == "worker"], imported
    assert {name for name in imported if name.split(".")[0] == "core"} <= {"core.logging_config"}, imported


def test_an_unconfigured_channel_is_a_permanent_not_configured_receipt(monkeypatch, tmp_path):
    from publishers.runner import run_publication
    monkeypatch.delenv("PUBLISHER_MOCK", raising=False)
    monkeypatch.setenv("JOB_ROOT", str(tmp_path))
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {})
    result = receipt(run_publication({"job_id": "job-2", "channel": "tiktok", "video_path": ""}, **hooks([])))
    assert result["status"] == "NOT_CONFIGURED"


def test_the_worker_delegates_to_the_runner_with_its_own_helpers(monkeypatch):
    import worker.role_executor as executor
    seen = {}

    def fake(task, **kwargs):
        seen.update(task=task, hooks=sorted(kwargs))
        return []
    monkeypatch.setattr("publishers.runner.run_publication", fake)
    assert executor.execute_publisher({"job_id": "j", "channel": "youtube"}) == []
    assert seen["hooks"] == ["artifact", "core_base_url", "fetch_input", "secret_source", "upload", "use_task_secrets"]


def test_configured_is_checked_with_the_task_secrets_from_core(monkeypatch, tmp_path):
    """#538 AC1: a disposable Publisher Node has no DATABASE_URL or env tokens; the channel
    is configured only by the per-task secrets CORE hands out."""
    from publishers.runner import run_publication
    from publishers.task_secrets import InMemoryTaskSecrets, task_secrets
    from publishers.tiktok import TikTokPublisher
    for name in ("PUBLISHER_MOCK", "DATABASE_URL", "TIKTOK_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOB_ROOT", str(tmp_path))
    monkeypatch.setattr("core.first_run.ensure_secret_store", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    publisher = TikTokPublisher()
    monkeypatch.setattr("publishers.LIVE_PUBLISHERS", {"tiktok": publisher})
    uploaded = []
    source = InMemoryTaskSecrets({"secrets": {"tiktok_access_token": "token"}})
    result = receipt(run_publication(
        {"job_id": "job-3", "channel": "tiktok", "video_path": ""},
        **{**hooks([]), "secret_source": lambda task: source, "use_task_secrets": task_secrets,
           "upload": lambda *args: uploaded.append(args) or {"status": "PUBLISHED", "id": "v1"}}))
    assert result["status"] == "PUBLISHED" and uploaded
