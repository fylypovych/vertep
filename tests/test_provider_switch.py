"""Issue #78: durable provider backend switch with verify-and-rollback.

Covers the provider half of "Voice preview та provider switch через перевірений
deployment/config flow, із погодженими negative paths": persist → apply →
verify → rollback, plus the HTTP contract the Web UI Settings screen uses.

The last block additionally closes the "Settings → real API → real executor"
chain of Issue #122 P8: the engine is applied through the HTTP endpoint the Web
UI calls, and the verify step is answered by an actual HTTP listener standing in
for the pinned runtime, so nothing about the successful path is stubbed inside
CORE.
"""

import base64
import contextlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.providers import provider_matrix
from core.app import app
from core.provider_switch import (
    ProviderSwitchError,
    apply_provider_overrides,
    effective_provider_config,
    load_provider_overrides,
    reset_provider_registry,
    switch_provider,
)

client = TestClient(app)


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point the override store at a temp dir and pin the initial backends."""
    config = tmp_path / "cfg"
    config.mkdir(parents=True, exist_ok=True)
    # Keep the First-Run guard open: the endpoints under test are admin API.
    (config / "installation.json").write_text(
        json.dumps({"completed_at": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    (config / "users.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "native")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    yield config
    reset_provider_registry()


def _overrides_file(config: Path) -> dict:
    path = config / "provider-overrides.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Core switch flow
# ---------------------------------------------------------------------------


def test_secret_reference_matches_node_provisioning_but_rejects_identity_drift(isolated_config, monkeypatch):
    from core.engine_config import engine_snapshot_fields, verify_engine_snapshot, EngineSnapshotMismatch
    from core.api.job_helpers import assembly_worker_mismatch
    from adapters.providers.video_engines import MoneyPrinterEngine
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    core_engine = MoneyPrinterEngine(url="http://runtime:8098", token="core-only-value")
    core_engine.secret_source = "secret-store"
    worker_engine = MoneyPrinterEngine(url="http://runtime:8098", token="worker-only-value")
    snapshot = engine_snapshot_fields(core_engine)
    reported = {**engine_snapshot_fields(worker_engine), "ready": True}
    verify_engine_snapshot(worker_engine, snapshot)
    assert assembly_worker_mismatch(reported, snapshot) is None
    for reference in ({"env": "OTHER_TOKEN", "configured": True, "source": "env"},
                      {"env": "MONEY_PRINTER_TOKEN", "configured": False, "source": "unset"},
                      {"env": "MONEY_PRINTER_TOKEN", "configured": True, "source": "unknown"}):
        changed = {**snapshot, "secret_reference": reference}
        with pytest.raises(EngineSnapshotMismatch):
            verify_engine_snapshot(worker_engine, changed)
        assert assembly_worker_mismatch(reported, changed) == "secret_reference_mismatch"


def test_failed_rollback_persistence_is_not_reported_as_restored(isolated_config, monkeypatch):
    import core.provider_switch as switching
    monkeypatch.delenv("PIPER_URL", raising=False)
    original = switching._write_overrides
    calls = []
    def write(overrides, **kwargs):
        calls.append(dict(overrides))
        if len(calls) == 2:
            raise OSError("controlled persistence outage")
        return original(overrides, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(switching, "_write_overrides", write)
        with pytest.raises(ProviderSwitchError) as failed:
            switch_provider("tts", "piper")
        assert failed.value.status_code == 503
        assert os.environ["TTS_PROVIDER"] == "none"
    apply_provider_overrides()
    assert load_provider_overrides() == {}
    assert json.loads((isolated_config / "provider-activation.json").read_text())["status"] == "ROLLED_BACK"


def test_switch_persists_applies_and_verifies(isolated_config):
    result = switch_provider("tts", "mock", actor="tester")
    assert result["changed"] is True
    assert result["env"] == "TTS_PROVIDER"
    assert result["matrix"]["tts"]["backend"] == "mock"
    assert provider_matrix()["tts"]["backend"] == "mock"

    stored = _overrides_file(isolated_config)
    assert stored["overrides"]["TTS_PROVIDER"] == "mock"
    assert stored["actor"] == "tester"
    assert stored["slot"] == "tts"
    assert load_provider_overrides() == {"TTS_PROVIDER": "mock"}


def test_switch_same_backend_is_a_noop(isolated_config):
    result = switch_provider("tts", "none", actor="tester")
    assert result["changed"] is False
    assert result["matrix"]["tts"]["backend"] == "none"
    assert _overrides_file(isolated_config) == {}


def test_overrides_are_reapplied_after_restart(isolated_config):
    switch_provider("tts", "mock", actor="tester")
    os.environ.pop("TTS_PROVIDER", None)
    assert provider_matrix()["tts"]["backend"] == "none"
    applied = apply_provider_overrides()
    assert applied == {"TTS_PROVIDER": "mock"}
    reset_provider_registry()
    assert provider_matrix()["tts"]["backend"] == "mock"
    # Issue i.0.0.1.17: the persisted switch is also the effective config after
    # a restart — matrix, overrides file and the executor subset delivered to
    # the Worker agree, and a failed activation never reports success.
    effective = effective_provider_config("tts")
    assert effective["backend"] == "mock"
    assert effective["persisted"] == "mock"
    assert effective["executor_config"] == {"TTS_PROVIDER": "mock"}
    assert effective["configured"] is True


def test_provider_executor_config_carries_only_allowlisted_non_secrets(isolated_config):
    from core.provider_switch import provider_executor_config

    switch_provider("tts", "mock", actor="tester")
    # A secret-looking extra override must never travel to the executor.
    config = isolated_config / "provider-overrides.json"
    stored = json.loads(config.read_text(encoding="utf-8"))
    stored["overrides"]["KOKORO_API_KEY"] = "super-secret"
    stored["overrides"]["MONEY_PRINTER_TOKEN"] = "super-secret"
    config.write_text(json.dumps(stored), encoding="utf-8")
    executor = provider_executor_config()
    assert executor == {"TTS_PROVIDER": "mock"}
    assert "KOKORO_API_KEY" not in executor
    assert "MONEY_PRINTER_TOKEN" not in executor


def test_heartbeat_delivers_provider_config_to_the_worker(isolated_config, monkeypatch):
    switch_provider("tts", "mock", actor="tester")
    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    response = client.post("/api/workers/heartbeat", json={"node_name": "voice-01"},
                           headers={"X-Vertep-Token": "worker-token"})
    assert response.status_code == 200
    assert response.json()["provider_config"] == {"TTS_PROVIDER": "mock"}


def test_worker_applies_heartbeat_provider_config(monkeypatch):
    from worker import service as worker_service

    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    applied = worker_service.apply_provider_config({"TTS_PROVIDER": "mock"})
    assert applied == {"TTS_PROVIDER": "mock"}
    assert os.environ["TTS_PROVIDER"] == "mock"
    assert worker_service.executor_provider_config() == {"TTS_PROVIDER": "mock"}
    # Non-allowlisted keys (in particular secrets) are never applied.
    assert worker_service.apply_provider_config({"KOKORO_API_KEY": "shh"}) == {}
    assert "KOKORO_API_KEY" not in os.environ


def test_preview_refuses_a_switch_that_missed_the_executor(isolated_config, monkeypatch):
    import core.api.models as models_api
    import core.provider_switch as provider_switch

    switch_provider("tts", "mock", actor="tester")
    # CORE persisted the switch, but the live process still runs the previous
    # backend (a Worker/CORE that has not re-applied yet): preview must refuse
    # instead of reporting success for an unobserved backend.
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.setattr(provider_switch, "load_provider_overrides",
                         lambda: {"TTS_PROVIDER": "mock"})
    monkeypatch.setattr(models_api, "list_voices", lambda: [{"name": "uk"}])
    monkeypatch.setattr(models_api, "synthesize_voice",
                         lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no synthesis")))
    response = client.post("/api/models/voices/preview", json={"text": "hi"})
    assert response.status_code == 409
    assert "executor" in response.json()["detail"]
    monkeypatch.setenv("TTS_PROVIDER", "mock")


def test_unconfigured_backend_rolls_back(isolated_config):
    before = provider_matrix()["llm"]["backend"]
    assert before == "ollama"
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("llm", "openai", actor="tester")
    assert exc.value.status_code == 409
    assert "not configured" in exc.value.message
    # Verification failed, so neither the environment nor the store changed.
    assert provider_matrix()["llm"]["backend"] == "ollama"
    assert load_provider_overrides() == {}
    assert _overrides_file(isolated_config)["overrides"] == {}


def test_unknown_backend_is_rejected_without_side_effects(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("tts", "does-not-exist", actor="tester")
    assert exc.value.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"
    assert load_provider_overrides() == {}


def test_missing_backend_is_rejected(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("tts", "", actor="tester")
    assert exc.value.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"


def test_unknown_slot_is_rejected(isolated_config):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider("nope", "mock", actor="tester")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("slot", ["assembly", "publisher"])
def test_immutable_slots_are_rejected(isolated_config, slot):
    with pytest.raises(ProviderSwitchError) as exc:
        switch_provider(slot, "anything", actor="tester")
    assert exc.value.status_code == 409
    assert "not switchable" in exc.value.message


def test_apply_provider_overrides_ignores_malformed_env_names(isolated_config):
    config = isolated_config
    config.mkdir(parents=True, exist_ok=True)
    (config / "provider-overrides.json").write_text(json.dumps({
        "overrides": {"TTS_PROVIDER": "mock", "bad name": "x", "lower_case": "y"},
    }), encoding="utf-8")
    applied = apply_provider_overrides()
    assert applied == {"TTS_PROVIDER": "mock"}
    assert "bad name" not in os.environ


# ---------------------------------------------------------------------------
# HTTP contract used by Settings -> Engines
# ---------------------------------------------------------------------------


def test_provider_matrix_endpoint_returns_options(isolated_config):
    response = client.get("/api/settings/providers")
    assert response.status_code == 200
    matrix = response.json()["matrix"]
    assert matrix["tts"]["backend"] == "none"
    assert "mock" in matrix["tts"]["options"]
    assert matrix["tts"]["env"] == "TTS_PROVIDER"


def test_provider_switch_endpoint_success(isolated_config):
    response = client.post("/api/settings/providers/tts", json={"backend": "mock"})
    assert response.status_code == 202, response.text
    assert response.json()["worker_activation"]["status"] == "AWAITING_WORKER"
    body = response.json()
    assert body["slot"] == "tts"
    assert body["backend"] == "mock"
    assert body["changed"] is True
    assert body["matrix"]["tts"]["backend"] == "none"
    assert body["matrix"]["tts"]["selected_backend"] == "mock"
    assert body["effective_config"]["backend"] == "none"
    assert body["effective_config"]["selected"] == "mock"
    assert body["effective_config"]["persisted"] == "mock"
    assert body["executor_config"] == {"TTS_PROVIDER": "mock"}
    assert _overrides_file(isolated_config)["overrides"]["TTS_PROVIDER"] == "mock"


def test_switch_reaches_the_executor_and_drives_preview_and_restart(isolated_config, monkeypatch):
    """Issue i.0.0.1.17: switch through the stock config/deployment flow.

    CORE → persisted ``provider-overrides.json`` → heartbeat ``provider_config``
    → Worker executor env → voice preview + effective config after restart.
    The switch/orchestration itself is real; only the external TTS transport is
    controlled (a loopback ``/synthesize`` + ``/voices`` runtime), never the
    config propagation under test.
    """
    import core.api.models as models_api
    from worker import role_executor, service as worker_service

    # A loopback TTS runtime standing in for the stock espeak service: the HTTP
    # transport is controlled, the CORE → Worker config flow is not.
    seen: dict = {}

    class _VoicesHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/voices":
                body = json.dumps({"voices": [{"id": "uk", "name": "uk", "engine": "espeak-ng"}],
                                   "models": ["espeak-ng"], "languages": ["uk"],
                                   # Issue #198/#165.1: the stand-in mirrors the
                                   # real espeak runtime catalog, which advertises
                                   # only the synthesizer it actually is.  "mock"
                                   # and "none" are not synthesizers and must not
                                   # be advertised as ready providers.
                                   "providers": ["espeak-ng"],
                                   "speed_range": {"min": 80, "max": 450},
                                   "engine": "espeak-ng"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path != "/synthesize":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            seen["request"] = json.loads(self.rfile.read(length) or b"{}")
            import base64 as _b64

            audio = b"RIFF\x04\x00\x00\x00WAVE-test"
            body = json.dumps({"audio_base64": _b64.b64encode(audio).decode(),
                               "mime_type": "audio/wav", "engine": "espeak-ng",
                               "voice": seen["request"].get("voice", "uk")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _VoicesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime_url = f"http://127.0.0.1:{server.server_address[1]}"
        monkeypatch.setenv("TTS_URL", runtime_url)
        # The worker-side executor reads the same TTS_URL env as production.
        monkeypatch.setattr(role_executor.os, "environ", os.environ)

        switched = switch_provider("tts", "mock", actor="tester")
        assert switched["executor_config"] == {"TTS_PROVIDER": "mock"}

        monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
        heartbeat = client.post("/api/workers/heartbeat", json={"node_name": "voice-01"},
                                headers={"X-Vertep-Token": "worker-token"}).json()
        assert heartbeat["provider_config"]["TTS_PROVIDER"] == "mock"

        # The Worker applies the heartbeat delivery to its own executor env.
        monkeypatch.delenv("TTS_PROVIDER", raising=False)
        applied = worker_service.apply_provider_config(heartbeat["provider_config"])
        assert applied["TTS_PROVIDER"] == "mock"
        assert worker_service.executor_provider_config()["TTS_PROVIDER"] == "mock"

        # Preview now proves the effective configuration end-to-end through the
        # real Worker executor path (role_executor → loopback runtime).
        preview = client.post("/api/models/voices/preview",
                              json={"text": "привіт", "voice": "uk"})
        assert preview.status_code == 200, preview.text
        assert preview.headers["content-type"].startswith("audio/")
        assert seen["request"]["voice"] == "uk"

        # The applied choice is reproduced after a restart of the CORE.
        os.environ.pop("TTS_PROVIDER", None)
        reset_provider_registry()
        apply_provider_overrides()
        reset_provider_registry()
        effective = effective_provider_config("tts")
        assert effective["backend"] == "mock"
        assert effective["persisted"] == "mock"
        assert effective["executor_config"]["TTS_PROVIDER"] == "mock"
    finally:
        server.shutdown()
        server.server_close()


def test_provider_switch_endpoint_unknown_slot(isolated_config):
    response = client.post("/api/settings/providers/assembly", json={"backend": "ffmpeg"})
    assert response.status_code == 409
    assert "not switchable" in response.json()["detail"]


def test_provider_switch_endpoint_invalid_backend(isolated_config):
    response = client.post("/api/settings/providers/tts", json={"backend": "nope"})
    assert response.status_code == 422
    assert provider_matrix()["tts"]["backend"] == "none"
    assert load_provider_overrides() == {}


def test_provider_switch_endpoint_missing_backend(isolated_config):
    response = client.post("/api/settings/providers/tts", json={})
    assert response.status_code == 422


def test_provider_switch_endpoint_rolls_back_unconfigured(isolated_config):
    response = client.post("/api/settings/providers/llm", json={"backend": "openai"})
    assert response.status_code == 409
    assert provider_matrix()["llm"]["backend"] == "ollama"
    assert load_provider_overrides() == {}


# ---------------------------------------------------------------------------
# HTTP contract used by Settings -> Движок відео (Issue #122 P8)
# ---------------------------------------------------------------------------


def test_the_video_engine_endpoint_renders_both_names_and_the_required_fields(
    isolated_config, monkeypatch
):
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://user:pass@runtime:8098/internal")
    monkeypatch.delenv("MONEY_PRINTER_TOKEN", raising=False)

    response = client.get("/api/settings/video-engine")

    assert response.status_code == 200
    body = response.json()
    labels = {option["id"]: option["label"] for option in body["options"]}
    assert labels["native"] == "Vertep Native"
    assert labels["money-printer"] == "MoneyPrinterTurbo"
    fields = {field["name"]: field for field in body["fields"]}
    assert fields["endpoint"]["env"] == "MONEY_PRINTER_URL"
    assert fields["endpoint"]["value"] == "http://runtime:8098"
    assert fields["token"]["env"] == "MONEY_PRINTER_TOKEN"
    assert fields["token"]["value"] is None
    assert body["values_exposed"] is False
    assert "pass" not in response.text


def test_a_switch_that_could_carry_a_secret_in_the_endpoint_is_refused(isolated_config):
    response = client.post("/api/settings/providers/video_engine", json={
        "backend": "money-printer", "endpoint": "http://user:pass@runtime:8098",
    })

    assert response.status_code == 422
    assert "credentials" in response.json()["detail"]
    assert load_provider_overrides() == {}


def test_switching_the_engine_is_refused_while_the_system_state_forbids_it(
    isolated_config, monkeypatch
):
    from core.system_state import SystemState, get_system_state, set_system_state

    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.UPDATING, "test rollout")
        response = client.post("/api/settings/providers/video_engine", json={
            "backend": "money-printer", "endpoint": "http://runtime:8098",
        })

        assert response.status_code == 423, response.text
        assert "configuration" in response.text
        assert load_provider_overrides() == {}
    finally:
        set_system_state(previous, "test restore")


def test_the_video_engine_endpoint_reflects_a_state_that_forbids_changes(isolated_config):
    from core.system_state import SystemState, get_system_state, set_system_state

    previous = get_system_state()["state"]
    try:
        set_system_state(SystemState.EMERGENCY, "test lock")
        body = client.get("/api/settings/video-engine").json()
        assert body["system_state"] == "EMERGENCY"
        assert body["change_allowed"] is False
    finally:
        set_system_state(previous, "test restore")


# ---------------------------------------------------------------------------
# P8: Settings → the real API → the real executor (Issue #122)
# ---------------------------------------------------------------------------


class _WrapperHandler(BaseHTTPRequestHandler):
    """Answers the readiness route the wrapper exposes on its own endpoint.

    The verify step of a switch talks to the runtime over HTTP, so the runtime in
    these tests is a real listener rather than a replaced method: an apply that
    succeeds here succeeded because something addressed by the configured endpoint
    proved its pinned inventory over a socket.
    """

    def do_GET(self):  # noqa: N802 - name imposed by BaseHTTPRequestHandler
        report = getattr(self.server, "report", None)
        if self.path.rstrip("/") != "/health" or not isinstance(report, dict):
            self.send_error(404)
            return
        body = json.dumps(report).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A003 - name imposed by the base class
        return


@contextlib.contextmanager
def _runtime_on_loopback(report: dict):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WrapperHandler)
    server.report = report
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _ready_report(*, bridge_schema_version: str | None = None,
                  upstream_commit: str | None = None) -> dict:
    """A readiness report of a runtime that satisfies the pinned contract."""
    from adapters.providers.base import BRIDGE_SCHEMA_VERSION, FIXED_SUBMIT_FIELDS
    from adapters.providers.video_engines import MONEY_PRINTER_CONTRACT

    pinned = MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[-1]
    return {
        "checks": {
            "upstream_auth_enforced": True,
            "submit_schema": list(FIXED_SUBMIT_FIELDS),
            "snapshot": {
                "upstream_commit": upstream_commit or pinned,
                "image_digest": "sha256:" + "a" * 64,
                "bridge_schema_version": bridge_schema_version or BRIDGE_SCHEMA_VERSION,
            },
        },
    }


def test_applying_the_engine_through_the_real_api_is_verified_by_the_runtime(isolated_config):
    with _runtime_on_loopback(_ready_report()) as endpoint:
        response = client.post("/api/settings/providers/video_engine", json={
            "backend": "money-printer", "endpoint": endpoint,
        })

    assert response.status_code == 202, response.text
    assert response.json()["worker_activation"]["status"] == "AWAITING_WORKER"
    body = response.json()
    assert body["changed"] is True
    assert body["backend"] == "money-printer"
    effective = body["effective_engine"]
    assert effective["effective"] == "native"
    assert effective["selected"] == "money-printer"
    assert effective["agree"] is False
    assert effective["ready"] is False
    assert effective["probed"] is True
    assert effective["endpoint"] == endpoint
    assert load_provider_overrides()["MONEY_PRINTER_URL"] == endpoint
    # The read model reports the same engine the apply proved.
    read_model = client.get("/api/settings/video-engine").json()
    assert read_model["selected"] == "money-printer" and read_model["effective"] == "native"
    assert read_model["config_revision"] == effective["config_revision"]


def test_a_runtime_that_cannot_prove_its_inventory_keeps_the_previous_engine(isolated_config):
    with _runtime_on_loopback(_ready_report(bridge_schema_version="0.0.0-drift")) as endpoint:
        response = client.post("/api/settings/providers/video_engine", json={
            "backend": "money-printer", "endpoint": endpoint,
        })

    assert response.status_code == 409, response.text
    assert load_provider_overrides() == {}
    read_model = client.get("/api/settings/video-engine").json()
    assert read_model["effective"] == "native"
    assert read_model["selected"] == "native"
    # Issue #127: the failed activation is not delivered to the executor either —
    # the Worker keeps the previous effective engine, and the API never reported
    # a success for the refused switch.
    from core.provider_switch import provider_executor_config

    delivered = provider_executor_config()
    assert "VERTEP_VIDEO_ENGINE" not in delivered
    assert "MONEY_PRINTER_URL" not in delivered


def test_the_applied_choice_is_reproduced_after_a_restart_of_the_core(isolated_config):
    with _runtime_on_loopback(_ready_report()) as endpoint:
        client.post("/api/settings/providers/video_engine", json={
            "backend": "money-printer", "endpoint": endpoint,
        })
        # A restart drops the live environment and the cached provider instances; only
        # the persisted choice remains, and it has to reproduce the same configuration.
        os.environ.pop("VERTEP_VIDEO_ENGINE", None)
        os.environ.pop("MONEY_PRINTER_URL", None)
        reset_provider_registry()
        assert provider_matrix()["video_engine"]["backend"] == "native"

        apply_provider_overrides()

        read_model = client.get("/api/settings/video-engine").json()
        assert read_model["selected"] == "money-printer" and read_model["effective"] == "native"
        assert read_model["worker_activation"]["status"] == "AWAITING_WORKER"
        assert read_model["agree"] is False
        assert read_model["endpoint"] == endpoint
        # The secret is restored as a reference only — no value is ever reported.
        assert read_model["secret"] == {
            "env": "MONEY_PRINTER_TOKEN", "configured": False, "source": "unset",
        }
        # Readiness is only claimed when the runtime is probed again, so the restored
        # choice is verified on the actual executor rather than assumed.
        assert read_model["probed"] is False
        assert client.get("/api/settings/video-engine?probe=true").json()["ready"] is False


# ---------------------------------------------------------------------------
# P9: the switch reaches a *separate* Worker process and survives its restart
# (Issue #127)
# ---------------------------------------------------------------------------


#: Runs in a fresh interpreter: it stands in for a Worker process on another
#: host that only receives the persisted, non-secret selection through the
#: stock heartbeat channel and must make the real executor observe it.
_WORKER_APPLY_EXECUTOR = r"""
import json, os
os.environ["NODE_CAPABILITIES"] = "speech_synthesis,video_generation"
from pathlib import Path
from worker import service as worker_service
from adapters.providers import providers as registry

# Fetch configuration from the CORE heartbeat endpoint (no prebuilt JSON file)
import httpx
core = os.environ["CORE_URL"]
token = os.environ["NODE_TOKEN"]
node = os.environ.get("WORKER_NODE", "voice-01")
role = "voice" if node.startswith("voice") else "gpu"
try:
    r = httpx.post(f"{core}/api/workers/heartbeat", json={"node_name": node, "role": role, "capabilities": ["speech_synthesis", "video_generation"]},
                   headers={"X-Vertep-Token": token}, timeout=10)
    r.raise_for_status()
    control = r.json()
except Exception as e:
    Path(os.environ["REPORT_PATH"]).write_text(json.dumps({"error": str(e)}), encoding="utf-8")
    raise
config = control.get("provider_config") or {}
applied = worker_service.apply_provider_config(config, control.get("provider_config_revision", ""))
observed = worker_service.executor_provider_config()
# The Worker reports what its executors really observe. A delivery this node
# could not activate is reported with the backend it restored, so CORE never
# records a success for a backend that never became effective here.
report_config = dict(observed)
report_config.update(applied)
# Mirror the real Worker loop (worker/service.py): after applying the delivered
# config it reports the effective executor config back on the next heartbeat.
ack = httpx.post(f"{core}/api/workers/heartbeat",
                 json={"node_name": node, "role": role, "provider_config": report_config, "capabilities": ["speech_synthesis", "video_generation"],
                       "provider_config_revision": worker_service._PROVIDER_CONFIG_REVISION,
                       "provider_engine_revision": worker_service.executor_engine_revision(),
                       "provider_activation_error": worker_service._PROVIDER_ACTIVATION_ERROR},
                 headers={"X-Vertep-Token": token}, timeout=10)
ack.raise_for_status()
engine = registry.video_engine()
Path(os.environ["REPORT_PATH"]).write_text(json.dumps({
    "applied": applied,
    "observed": report_config,
    "video_engine_env": os.environ.get("VERTEP_VIDEO_ENGINE"),
    "money_printer_url": os.environ.get("MONEY_PRINTER_URL"),
    "engine_class": type(engine).__name__,
}), encoding="utf-8")
"""


def _run_worker_apply(core_url: str, node_name: str, run_dir: Path,
                      drop_env: tuple[str, ...] = ()) -> dict:
    """Run a separate Worker process that fetches its config from CORE.

    The subprocess inherits no engine selection of its own, so whatever it ends
    up resolving must have come from the ``provider_config`` CORE served on the
    stock heartbeat response — the delivery channel the real Worker loop uses,
    not a locally written JSON fixture (Issue #170.3 / #181.2).  ``drop_env``
    removes node-local provider configuration the subprocess must not inherit.
    """
    import subprocess
    import sys

    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    env = dict(os.environ)
    env["CORE_URL"] = core_url
    env["NODE_TOKEN"] = os.getenv("NODE_API_TOKEN", "")
    env["WORKER_NODE"] = node_name
    env["REPORT_PATH"] = str(report_path)
    for key in ("VERTEP_VIDEO_ENGINE", "MONEY_PRINTER_URL", *drop_env):
        env.pop(key, None)
    subprocess.run([sys.executable, "-c", _WORKER_APPLY_EXECUTOR],
                   cwd=str(Path(__file__).resolve().parents[1]),
                   env=env, check=True, capture_output=True, text=True)
    return json.loads(report_path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def _live_core():
    """Serve the real CORE app over a loopback socket with uvicorn.

    A separate Worker process cannot talk to an in-process ``TestClient``; it
    needs a real socket.  Served in this process, the uvicorn CORE shares the
    same node store, so CORE-side reads observe exactly what the out-of-process
    Worker reported back (Issue #181.2).
    """
    import base64
    import socket
    import time

    import httpx
    import uvicorn

    from core.app import app as core_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(core_app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    password = os.getenv("ADMIN_PASSWORD", "")
    auth = "Basic " + base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        yield base, auth
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_separate_worker_process_applies_and_reapplies_the_switched_engine(
        isolated_config, tmp_path, monkeypatch):
    import httpx

    from core.provider_switch import (
        effective_provider_config,
        executor_backend_mismatches,
        provider_executor_config,
    )

    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    with _live_core() as (base, auth), _runtime_on_loopback(_ready_report()) as endpoint:
        response = httpx.post(f"{base}/api/settings/providers/video_engine",
                              json={"backend": "money-printer", "endpoint": endpoint},
                              headers={"Authorization": auth}, timeout=30)
        assert response.status_code == 202, response.text
        assert provider_executor_config()["VERTEP_VIDEO_ENGINE"] == "money-printer"
        # Only bare selection/endpoint values travel to the executor: no secret.
        assert "MONEY_PRINTER_TOKEN" not in provider_executor_config()

        first = _run_worker_apply(base, "gpu-01", tmp_path / "run1")
        assert first["engine_class"] == "MoneyPrinterEngine"
        assert first["video_engine_env"] == "money-printer"
        assert first["observed"]["VERTEP_VIDEO_ENGINE"] == "money-printer"
        assert first["observed"]["MONEY_PRINTER_URL"] == endpoint
        # The out-of-process Worker ack reaches CORE and verifies the effective
        # configuration: no mismatch, and the ack-aware read model agrees.
        assert executor_backend_mismatches("video_engine") == []
        effective = effective_provider_config("video_engine", node_name="gpu-01")
        assert effective["backend"] == "money-printer"
        assert effective["worker_acks"]["gpu-01"]["VERTEP_VIDEO_ENGINE"] == "money-printer"

        # A restarted Worker process, started clean again, re-applies the same
        # persisted selection and resolves the same engine — the switch is not lost.
        second = _run_worker_apply(base, "gpu-01", tmp_path / "run2")
        assert second == first


def test_preview_refuses_a_worker_that_never_confirmed_the_switch(isolated_config):
    """A stale Worker ack is a failed activation: preview must not claim success."""
    from core.api.workers import store
    from core.provider_switch import effective_provider_config, executor_backend_mismatches

    switch_provider("tts", "mock", actor="tester")
    # A voice Worker has reported back, but still names the previous backend.
    store.workers["voice-01"] = {
        "node_name": "voice-01", "role": "voice",
        "provider_config": {"TTS_PROVIDER": "none"},
    }
    try:
        assert executor_backend_mismatches("tts") == [
            {"node_name": "voice-01", "observed": "none", "expected": "mock"}]
        assert effective_provider_config("tts")["worker_acks"]["voice-01"]["TTS_PROVIDER"] == "none"
        with pytest.raises(ProviderSwitchError):
            effective_provider_config("tts", node_name="voice-01")

        # The preview gate refuses: it must not synthesize through a backend this
        # Worker never observed.
        response = client.post("/api/models/voices/preview", json={"text": "hi"})
        assert response.status_code == 409
        assert "voice-01" in response.json()["detail"]

        # Once the Worker reports the switched backend, the gate clears.
        store.workers["voice-01"]["provider_config"] = {"TTS_PROVIDER": "mock"}
        assert executor_backend_mismatches("tts") == []
        assert effective_provider_config("tts", node_name="voice-01")["backend"] == "mock"
    finally:
        store.workers.pop("voice-01", None)


def test_a_refused_engine_switch_keeps_the_previous_engine_on_the_worker_too(
        isolated_config, tmp_path, monkeypatch):
    """Failed activation rolls back on both ends — CORE and the separate Worker."""
    import httpx

    from core.provider_switch import provider_executor_config

    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    with _live_core() as (base, auth), \
            _runtime_on_loopback(_ready_report(bridge_schema_version="0.0.0-drift")) as endpoint:
        response = httpx.post(f"{base}/api/settings/providers/video_engine",
                              json={"backend": "money-printer", "endpoint": endpoint},
                              headers={"Authorization": auth}, timeout=30)
        assert response.status_code == 409, response.text
        # CORE rolled back: nothing about the failed engine is delivered.
        delivered = provider_executor_config()
        assert "VERTEP_VIDEO_ENGINE" not in delivered
        assert "MONEY_PRINTER_URL" not in delivered

        # The separate Worker, started clean, observes the same previous engine.
        report = _run_worker_apply(base, "gpu-01", tmp_path / "run")
        assert report["observed"].get("VERTEP_VIDEO_ENGINE") is None
        assert report["engine_class"] != "MoneyPrinterEngine"


# ---------------------------------------------------------------------------
# Issue #170.1/#170.2/#181.2: the switched provider serves a real task
# ---------------------------------------------------------------------------


class _TTSRuntimeHandler(BaseHTTPRequestHandler):
    """Loopback TTS runtime that records the synthesis request it receives."""

    def log_message(self, *args):
        return

    def do_POST(self):
        if self.path != "/synthesize":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)
        audio = b"RIFF\x04\x00\x00\x00WAVE-test"
        body = json.dumps({"audio_base64": base64.b64encode(audio).decode(),
                           "mime_type": "audio/wav", "engine": "espeak-ng"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _tts_runtime_on_loopback():
    """Serve the TTS runtime the Worker task talks to, and record its requests."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TTSRuntimeHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


#: Runs in a fresh interpreter: it applies the configuration CORE delivered on
#: the stock heartbeat channel and then executes a real task through the stock
#: executor path, so the report proves that the task ran with the switched
#: provider in the process that actually executes tasks.
_WORKER_EXECUTE_TASK = r"""
import json, os
os.environ["NODE_CAPABILITIES"] = "speech_synthesis,video_generation"
from pathlib import Path
import httpx
from worker import service as worker_service
from worker.role_executor import execute_role_task

core = os.environ["CORE_URL"]
token = os.environ["NODE_TOKEN"]
node = os.environ.get("WORKER_NODE", "voice-01")
report_path = os.environ["REPORT_PATH"]
report = {}
try:
    control = httpx.post(f"{core}/api/workers/heartbeat", json={"node_name": node},
                         headers={"X-Vertep-Token": token}, timeout=10)
    control.raise_for_status()
    applied = worker_service.apply_provider_config(control.json().get("provider_config") or {}, control.json().get("provider_config_revision", ""))
    observed = worker_service.executor_provider_config()
    task = json.loads(os.environ["TASK_PAYLOAD"])
    artifacts = execute_role_task("voice", task)
    ack = httpx.post(f"{core}/api/workers/heartbeat",
                     json={"node_name": node, "role": "voice", "provider_config": observed, "capabilities": ["speech_synthesis", "video_generation"],
                           "provider_config_revision": worker_service._PROVIDER_CONFIG_REVISION,
                           "provider_engine_revision": worker_service.executor_engine_revision(),
                           "provider_activation_error": worker_service._PROVIDER_ACTIVATION_ERROR},
                     headers={"X-Vertep-Token": token}, timeout=10)
    ack.raise_for_status()
    contract = (artifacts[0].get("contract") or {}) if artifacts else {}
    report = {
        "applied": applied,
        "observed": observed,
        "artifact_count": len(artifacts),
        "artifact_kind": artifacts[0].get("kind") if artifacts else None,
        "contract_provider": contract.get("provider"),
        "contract_voice": contract.get("voice"),
    }
except Exception as error:
    report = {"error": f"{type(error).__name__}: {error}"}
Path(report_path).write_text(json.dumps(report), encoding="utf-8")
if "error" in report:
    raise SystemExit(1)
"""


def _run_worker_task(core_url: str, node_name: str, run_dir: Path, task: dict) -> dict:
    """Run a separate Worker process that executes a task with the delivered config."""
    import subprocess
    import sys

    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    env = dict(os.environ)
    env["CORE_URL"] = core_url
    env["NODE_TOKEN"] = os.getenv("NODE_API_TOKEN", "")
    env["WORKER_NODE"] = node_name
    env["REPORT_PATH"] = str(report_path)
    env["TASK_PAYLOAD"] = json.dumps(task, ensure_ascii=False)
    # The Worker starts without a provider selection of its own: everything it
    # uses has to arrive from CORE through the heartbeat channel.
    env.pop("TTS_PROVIDER", None)
    result = subprocess.run([sys.executable, "-c", _WORKER_EXECUTE_TASK],
                            cwd=str(Path(__file__).resolve().parents[1]),
                            env=env, capture_output=True, text=True)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    if result.returncode != 0:
        raise AssertionError(f"worker task failed: {report} {result.stderr[-800:]}")
    return report


def test_a_task_runs_in_the_worker_process_that_applied_the_switched_provider(
        isolated_config, tmp_path, monkeypatch):
    """Issue #170.1/#170.2/#181.2: the switched provider serves a real task.

    CORE → persisted selection → heartbeat delivery → Worker apply → the stock
    executor runs the task → the runtime receives the new provider.  The
    Worker process starts without a selection of its own, so the only source of
    the switched backend is the configuration CORE delivered.
    """
    import httpx

    from core.provider_switch import effective_provider_config

    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    task = {"task": "voice", "topic": "Привіт Vertep", "voice": "uk",
            "scene_id": "scene-matrix", "character_id": "matrix_char",
            "language": "uk", "speed": 150}
    with _live_core() as (base, auth), _tts_runtime_on_loopback() as (runtime_url, requests):
        monkeypatch.setenv("TTS_URL", runtime_url)
        response = httpx.post(f"{base}/api/settings/providers/tts",
                              json={"backend": "mock"},
                              headers={"Authorization": auth}, timeout=30)
        assert response.status_code == 202, response.text
        assert response.json()["executor_config"]["TTS_PROVIDER"] == "mock"

        report = _run_worker_task(base, "voice-01", tmp_path / "run1", task)
        assert report["applied"] == {"TTS_PROVIDER": "mock"}
        assert report["observed"]["TTS_PROVIDER"] == "mock"
        assert report["artifact_count"] == 1
        assert report["artifact_kind"] == "audio"
        assert report["contract_provider"] == "mock"
        assert report["contract_voice"] == "uk"
        assert requests, "the runtime received no synthesis request"
        assert requests[-1]["provider"] == "mock"
        assert requests[-1]["text"] == "Привіт Vertep"

        effective = effective_provider_config("tts", node_name="voice-01")
        assert effective["backend"] == "mock"
        assert effective["worker_acks"]["voice-01"]["TTS_PROVIDER"] == "mock"

        # A restarted Worker process applies the same persisted selection again
        # and runs the task with it: the switch is not lost with the process.
        restarted = _run_worker_task(base, "voice-01", tmp_path / "run2", task)
        assert restarted["observed"]["TTS_PROVIDER"] == "mock"
        assert restarted["contract_provider"] == "mock"
        assert requests[-1]["provider"] == "mock"


def test_a_worker_that_cannot_activate_the_switched_provider_keeps_its_previous_config(
        isolated_config, tmp_path, monkeypatch):
    """Issue #181.3: a failed Worker activation restores the previous config.

    The switched backend is usable on CORE but not on the Worker node, because
    the address of the provider runtime is node-local configuration.  The Worker
    therefore cannot activate it: it restores the backend it had before the
    delivery, reports what it really observes, and CORE refuses to report the
    slot as effective for that node.
    """
    import httpx

    from core.provider_switch import effective_provider_config, executor_backend_mismatches

    monkeypatch.setenv("NODE_API_TOKEN", "worker-token")
    monkeypatch.setenv("PIPER_URL", "http://piper-core:5000")
    with _live_core() as (base, auth):
        response = httpx.post(f"{base}/api/settings/providers/tts",
                              json={"backend": "piper"},
                              headers={"Authorization": auth}, timeout=30)
        assert response.status_code == 202, response.text
        assert response.json()["worker_activation"]["status"] == "AWAITING_WORKER"
        assert response.json()["effective_config"]["backend"] == "none"
        assert response.json()["effective_config"]["selected"] == "piper"

        # The Worker node knows no runtime address and starts without a
        # selection of its own, so it cannot use Piper.
        report = _run_worker_apply(base, "voice-01", tmp_path / "run",
                                   drop_env=("PIPER_URL", "TTS_PROVIDER"))
        assert report["observed"]["TTS_PROVIDER"] == "none"

        # CORE sees the contradiction and never claims success for this node.
        assert executor_backend_mismatches("tts") == []
        assert effective_provider_config("tts", node_name="voice-01")["backend"] == "none"
        assert load_provider_overrides() == {}

        # The read model states the failed activation instead of reporting the
        # switched provider as effective on the node that runs the tasks.
        read_model = client.get("/api/settings/providers").json()
        assert read_model["activation"]["status"] == "ROLLED_BACK"
        assert read_model["matrix"]["tts"]["backend"] == "none"
        assert read_model["worker_activation"]["tts"]["mismatches"] == []
        assert read_model["worker_acks"]["voice-01"]["TTS_PROVIDER"] == "none"
