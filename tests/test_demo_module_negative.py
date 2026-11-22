"""i.0.0.4.95 (#495) AC5: the demo module never becomes falsely READY and never breaks CORE.

Real CORE WorkflowRuntime + lifecycle + registry and the real demo package
(built with a fixed image digest; no container is needed for these refusals).
"""

import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.module.lifecycle import LifecycleError

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "registry.example/vertep/demo-notes@sha256:" + "c" * 64


def _build():
    spec = importlib.util.spec_from_file_location("demo_build", ROOT / "examples/modules/demo-notes/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "publishers.json").write_text(json.dumps({"vertep-demo": {
        "keys": {"demo-k1": key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()},
        "repositories": ["github.com/fylypovych/vertep"]}}))
    dag = tmp_path / "dag"
    dag.mkdir()
    (dag / "configured.json").write_text(json.dumps({"name": "C", "version": "1.0.0", "tasks": [{"id": "a", "type": "text_generation"}]}))
    for name, value in {"VERTEP_MODULE_TRUST_ROOT": str(trust), "VERTEP_MODULE_LIFECYCLE_ROOT": str(tmp_path / "lc"),
                        "VERTEP_MODULE_PLACEMENT_FILE": str(tmp_path / "p.json"), "WORKFLOW_STATE_ROOT": str(tmp_path / "s"),
                        "VERTEP_MODULE_AUDIT_FILE": str(tmp_path / "audit.jsonl")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VERTEP_MODULE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from core.workflow.runtime import WorkflowRuntime
    instance = WorkflowRuntime(dag)
    build = _build()

    def package(version="1.0.0", **changes):
        signed = build.package(version, IMAGE, key, "demo-k1", "vertep-demo", "github.com/fylypovych/vertep")
        if changes:
            manifest = {**signed["manifest"], **changes, "health": {"self_test": "health", "timeout_seconds": 1}}
            content = {"manifest": manifest, "files": signed["files"]}
            import base64
            raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            signed = {**content, "signer": "demo-k1", "signature": base64.b64encode(key.sign(raw)).decode()}
        return signed
    instance.package = package
    return instance


def report(runtime, ready, version="1.0.0"):
    manifest = runtime.package(version)["manifest"]
    runtime.modules.report_node("demo-node", [{**manifest, "ready": ready, "endpoint": "http://127.0.0.1:9"}])


def test_missing_dependency_and_incompatible_version_are_refused_before_install(runtime):
    with pytest.raises(LifecycleError, match="dependencies"):
        runtime.lifecycle.install(runtime.package(dependencies=[{"name": "absent-module", "min_version": "1.0.0"}]))
    with pytest.raises(LifecycleError, match="dependencies"):
        runtime.lifecycle.install(runtime.package(core_compatibility={"min": "9.0.0.0"}))
    assert runtime.lifecycle.records() == {}


@pytest.mark.parametrize("case", ["no_permission", "no_readiness", "self_test_failed", "no_database"])
def test_activation_refusals_leave_no_false_ready(runtime, case):
    lifecycle, registry = runtime.lifecycle, runtime.modules
    lifecycle.install(runtime.package(migrations=[]))  # no schema needed for the negatives
    registry.set_placement("demo-notes", ["demo-node"])
    if case != "no_permission":
        registry.approve_permissions("demo-notes", ["demo-notes.data"])
    if case == "self_test_failed":
        report(runtime, ready=False)  # /health answered not ready
    if case == "no_database":
        lifecycle.uninstall("demo-notes", confirmed=True)
        lifecycle.install(runtime.package())  # declares migrations; no database configured
        registry.approve_permissions("demo-notes", ["demo-notes.data"])
    with pytest.raises(LifecycleError):
        lifecycle.activate("demo-notes")
    assert lifecycle.record("demo-notes")["state"] == "INSTALLED"
    assert registry.ready_instances("demo-notes") == []
    assert not (lifecycle.root / "journal/demo-notes.json").exists()


def test_absent_or_failed_module_does_not_block_the_platform(runtime, monkeypatch):
    from fastapi.testclient import TestClient
    from core.app import app
    monkeypatch.setattr("core.workflow.runtime.workflow_runtime", lambda: runtime)
    client = TestClient(app)
    assert client.get("/api/modules/ui-extensions").json() == {"extensions": []}
    assert client.get("/api/modules/demo-notes/v1/notes").status_code == 404
    runtime.lifecycle.install(runtime.package(migrations=[]))
    runtime.modules.set_placement("demo-notes", ["demo-node"])
    runtime.modules.approve_permissions("demo-notes", ["demo-notes.data"])
    report(runtime, ready=True)
    assert runtime.modules.ready_instances("demo-notes") == []  # installed, not activated
    assert client.get("/api/modules/demo-notes/v1/notes").status_code == 404
    assert client.get("/api/health").status_code == 200
    job = runtime.create("configured", {"topic": "platform keeps working"})
    assert runtime.engine.get_job_status(job)["status"] == "IN_PROGRESS"


def test_a_refused_reactivation_keeps_a_deactivated_module_inactive(runtime):
    """Regression of 0.0.4.33: a deactivated module whose activation is refused became FAILED."""
    lifecycle, registry = runtime.lifecycle, runtime.modules
    lifecycle.install(runtime.package(migrations=[]))
    registry.set_placement("demo-notes", ["demo-node"])
    registry.approve_permissions("demo-notes", ["demo-notes.data"])
    report(runtime, ready=True)
    lifecycle.activate("demo-notes")
    lifecycle.deactivate("demo-notes")
    report(runtime, ready=False)  # the node is not ready yet
    with pytest.raises(LifecycleError):
        lifecycle.activate("demo-notes")
    assert lifecycle.record("demo-notes")["state"] == "INACTIVE"
