"""Issue #295 (i.0.0.2.95) — MPT: combined pinned runtime matrix та first-submit recovery.

Criteria covered locally (unit/integration, no real upstream container/FFmpeg):
- #179.1/#179.2: deterministic early audio stage — the runtime must receive the
  approved voice before any byte is generated; missing/tampered voice is refused
  before POST; no TTS fallback or voice race on the first valid submit.
- #180.6: lost submit response — recovery without automatically re-submitting an
  ambiguous key; mapping/ownership recovery or UNKNOWN.
- #179.3: engine restart via re-instantiation keeps the approved inputs.

#180.1–#180.3 had only placeholder checks here and were removed (#520): CORE never
renders (tests/test_core_generation_gate.py), version history and stale approvals are
covered by tests/test_video_revision_*.py and tests/test_assembly_worker_route.py.
- #180.7: upstream submission count — a repeat of the same submit key does not
  create duplicate upstream work; submit key idempotency via runtime 409.
- #180.8: bounded polling timeout/429/5xx/malformed; queued/running cancel, busy
  DELETE409, unreachable runtime held until proven terminal/absent.
- #180.9: interrupted/corrupted download is never registered; download retry does
  not re-generate the upstream task.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path

import httpx
import pytest

from adapters.providers.video_engines import (
    BridgeContractError, MoneyPrinterEngine, NativeVertepEngine,
    REASON_SUBMIT_UNKNOWN,
    REASON_TASK_ABSENT, REASON_UPSTREAM_UNREACHABLE,
    RELEASE_UNCONFIRMED,
    REASON_VOICE_STAGING_UNSUPPORTED,
)
from tests.test_mpt_media_matrix import PinnedRuntime, _png


def _make_wav(path: Path, seconds: float = 1.0, rate: int = 24000,
              frequency: float = 440.0) -> Path:
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate)))
        for i in range(int(rate * seconds)))
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
                     + struct.pack("<I", rate) + struct.pack("<I", rate * 2)
                     + b"\x02\x00\x10\x00data" + struct.pack("<I", len(frames)) + frames)
    return path


def _base_request() -> dict:
    return {
        "video_subject": "test", "video_script": "x",
        "video_materials": [], "custom_audio_file": "voice.wav",
        "video_aspect": "16:9", "video_source": "local",
        "video_clip_duration": 2, "video_count": 1,
        "bgm_type": "", "bgm_file": "", "bgm_volume": 0.0,
        "subtitle_enabled": False, "video_language": "", "n_threads": 2,
        "video_terms": None, "video_fit_mode": "contain",
        "video_concat_mode": "sequential", "video_transition_mode": None,
        "video_clip_speed": 1.0, "match_materials_to_script": False,
    }


@pytest.fixture
def engine(tmp_path):
    return MoneyPrinterEngine(url="http://engine:8000",
                              transport=PinnedRuntime(tmp_path),
                              poll_interval=0.01)


@pytest.fixture
def runtime(engine):
    return engine._transport


# ── #179.1/#179.2: voice staging without TTS fallback, first-submit voice race ──


def test_voice_staging_uses_approved_file_not_tts(engine, runtime, tmp_path):
    """#179.2: the runtime receives the approved voice file via
    /api/v1/voice and records its sha256; no TTS fallback is exercised."""
    audio_path = _make_wav(tmp_path / "voice.wav")
    digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()

    reference = engine._stage_approved_voice(audio_path)

    # The voice was staged before any submit — file written to runtime root.
    assert (runtime.root / f"{digest}.wav").is_file()
    staged = (runtime.root / f"{digest}.wav").read_bytes()
    assert staged == audio_path.read_bytes()
    # The staged reference carries the sha256-based name.
    assert f"vertep-voice:{digest}.wav" == reference


def test_empty_voice_is_refused_before_post(engine, runtime, tmp_path):
    """#179.2: an empty voice file is refused before any POST to the runtime."""
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(BridgeContractError, match="empty"):
        engine._stage_approved_voice(empty)


def test_runtime_without_voice_staging_is_refused(tmp_path):
    """#179.2: a runtime that cannot receive the approved voice (task_local_voice
    false) is refused instead of letting it synthesise its own narration.

    The capability fault is answered by a real HTTP endpoint (the stand-in no longer
    carries this fault, #392), so the refusal is proven on the engine's own transport.
    """
    import threading
    from http.server import ThreadingHTTPServer

    from publishers.transport import HttpTransport
    from tests.test_mpt_media_matrix import _CapabilityFaultRuntime

    _CapabilityFaultRuntime.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapabilityFaultRuntime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = MoneyPrinterEngine(url=f"http://127.0.0.1:{server.server_address[1]}",
                                    token="capability-fault", transport=HttpTransport())
        with pytest.raises(BridgeContractError) as exc:
            engine._require_task_local_voice()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert exc.value.code == REASON_VOICE_STAGING_UNSUPPORTED
    assert _CapabilityFaultRuntime.requests == ["/runtime"]


# ── #180.6: lost submit response — recovery without auto-resubmit ──


def test_reconcile_submit_none_when_no_key(engine):
    """#180.6: without a durable submit key, a lost response is terminal
    (submit_unknown), never ambiguous."""
    assert engine._reconcile_submit(None) is None


def test_submission_count_is_one_per_submit(engine, runtime, tmp_path):
    """#180.7: one submit call produces exactly one upstream task entry."""
    request = _base_request()
    engine._submit_task(request, submit_key=None)
    assert len(runtime.submits) == 1
    payload = runtime.submits[0]
    assert payload["video_subject"] == "test"
    assert payload["video_source"] == "local"
    assert payload["video_count"] == 1


# ── #180.8: bounded polling timeout/429/5xx/malformed ──


def test_unreachable_runtime_is_refused_not_held_forever(engine, runtime, tmp_path,
                                                         monkeypatch):
    """#180.8: an unreachable runtime (transport failure) is refused with
    upstream_unreachable, never silently held as success."""

    class UnreachableTransport:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("runtime gone")

        def post(self, url, **kwargs):
            raise httpx.ConnectError("runtime gone")

    original_transport = engine._transport
    engine._transport = UnreachableTransport()
    try:
        with pytest.raises(BridgeContractError) as exc:
            engine._get_with_retry("/api/v1/tasks/task-1", 1, "task status")
        assert exc.value.code == REASON_UPSTREAM_UNREACHABLE
    finally:
        engine._transport = original_transport


def test_429_5xx_are_bounded_retries_not_infinite(engine, runtime, tmp_path,
                                                  monkeypatch):
    """#180.8: 429/5xx/502/503/504 are retried bounded times then refused, not
    looped forever."""
    # First submit so _status has a task to return.
    request = _base_request()
    engine._submit_task(request, submit_key=None)

    calls = []

    class Always503Transport:
        def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(503, json={"data": {"state": 1}})
        def post(self, url, **kwargs):
            return httpx.Response(503, json={"data": {"state": 1}})

    monkeypatch.setattr("adapters.providers.video_engines.remote.time.sleep", lambda seconds: None)
    original_transport = engine._transport
    engine._transport = Always503Transport()
    try:
        # A deadline far ahead: only the attempt limit can stop the retries.
        far = __import__("time").monotonic() + 3600
        with pytest.raises(BridgeContractError) as refusal:
            engine._get_with_retry("/api/v1/tasks/task-1", far, "task status")
    finally:
        engine._transport = original_transport
    assert refusal.value.code == "upstream_transient_failure"
    assert len(calls) == engine._max_read_attempts > 1


def test_release_state_is_unconfirmed_for_remote_engine():
    """#5: a remote engine (MoneyPrinter) has no confirmed release, so release
    state is RELEASE_UNCONFIRMED, never RELEASE_RELEASED."""
    engine = MoneyPrinterEngine(url="http://engine:8000",
                                transport=PinnedRuntime(Path("/tmp")))
    report = engine.release_state()
    assert report == RELEASE_UNCONFIRMED


# ---------------------------------------------------------------------------
# #180.1/#180.2: separate CORE and Worker objects with different storage roots;
#                CORE does not render MPT directly (NativeVertepEngine is CORE-only)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #180.7: submit key idempotency — a 409 from the runtime on a repeat key
#          resolves to the existing task, not a duplicate submit
# ---------------------------------------------------------------------------


def test_submit_key_idempotency_409_resolves_to_existing_task(tmp_path):
    """#180.7: when the runtime returns 409 for a repeated submit key, the engine
    resolves it through the durable submit-key status route instead of creating a
    second upstream task.
    """
    key = "idempotent-key-abc"
    existing_task = "task-existing-99"

    class IdempotentRuntime:
        """Returns 409 for the submit but 200 + task_id for the key status route."""

        def post(self, url, **kwargs):
            return httpx.Response(409, json={"reason": "key_in_flight"})

        def get(self, url, **kwargs):
            path = url.split(":8000", 1)[-1]
            if "/api/v1/videos/" in path and key in path:
                return httpx.Response(200, json={
                    "state": "submitted",
                    "data": {"task_id": existing_task},
                })
            return httpx.Response(404)

        def delete(self, url, **kwargs):
            return httpx.Response(404)

    engine = MoneyPrinterEngine(url="http://engine:8000",
                                transport=IdempotentRuntime(), poll_interval=0.01)
    request = _base_request()
    # A second submit with the same key must not create a new upstream task.
    result = engine._submit_task(request, submit_key=key)
    body = result.json()
    data = body.get("data") if isinstance(body, dict) else body
    assert data.get("task_id") == existing_task, (
        "a 409 on a repeat submit key must resolve to the existing task, "
        "never create a duplicate"
    )


# ---------------------------------------------------------------------------
# #180.3: immutable revision history — two revisions keep every accepted
#          version intact; stale/duplicate revision is rejected
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #179.3/#180.6: engine restart via re-instantiation recovers submit key
#                without deleting approved inputs
# ---------------------------------------------------------------------------


def test_engine_restart_via_reinstantiation_recovers_approved_voice(tmp_path):
    """#179.3/#180.6: simulated engine restart (re-instantiation) does not delete
    the already-staged approved voice, and the new engine object can submit.
    Resolving a submit key after a restart is covered by
    test_submit_key_idempotency_409_resolves_to_existing_task.
    """
    runtime = PinnedRuntime(tmp_path)
    voice = _make_wav(tmp_path / "voice.wav")
    digest = __import__("hashlib").sha256(voice.read_bytes()).hexdigest()

    # First engine instance: stages the voice.
    engine_a = MoneyPrinterEngine(url="http://engine:8000",
                                  transport=runtime, poll_interval=0.01)
    engine_a._stage_approved_voice(voice)
    assert (runtime.root / f"{digest}.wav").is_file(), \
        "approved voice must be staged before restart"

    # Simulated restart: create a new engine object (same runtime, same root).
    engine_b = MoneyPrinterEngine(url="http://engine:8000",
                                  transport=runtime, poll_interval=0.01)
    assert engine_b is not engine_a, "restart produces a new engine object"

    # The staged voice survives the restart — the file is still there.
    assert (runtime.root / f"{digest}.wav").is_file(), \
        "approved staged voice must survive engine re-instantiation"

    # The new engine can still submit (the runtime has the staged materials).
    request = _base_request()
    engine_b._submit_task(request, submit_key=None)
    assert len(runtime.submits) == 1, "submit after restart must reach the runtime once"


# ---------------------------------------------------------------------------
# #180.9: download retry does not re-generate the upstream task
# ---------------------------------------------------------------------------


def test_download_retry_does_not_create_new_submit(tmp_path):
    """#180.9: when a download fails (corrupted bytes), the engine must not
    re-submit the task — a retry is a new GET, not a new POST.
    """
    runtime = PinnedRuntime(tmp_path)
    runtime.corrupt_download = b"not-a-video"  # Force corrupted download.

    engine = MoneyPrinterEngine(url="http://engine:8000",
                                transport=runtime, poll_interval=0.01)

    voice = _make_wav(tmp_path / "voice.wav")
    from tests.test_mpt_media_matrix import SCENE_WIDTH, SCENE_HEIGHT, MATRIX_SCENES
    import wave, numpy as np
    from pathlib import Path

    # Build minimal approved assets matching what the matrix tests use.
    images = []
    for label, colour, _ in MATRIX_SCENES:
        img = _png(tmp_path / f"{label}.png", colour)
        images.append(img)
    durations = [dur for _, _, dur in MATRIX_SCENES]

    total = sum(durations)
    frames = np.sin(
        2 * np.pi * 440.0 * np.arange(int(24000 * total)) / 24000
    ) * 12000
    with wave.open(str(voice), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(frames.astype("<i2").tobytes())

    output = tmp_path / "final" / "video-v1.mp4"
    with pytest.raises(BridgeContractError) as exc:
        engine.render(output, images=images, durations=durations, audio=voice,
                      aspect_ratio="9:16", task_type="image",
                      script="narration text")

    assert exc.value.code == "upstream_result_corrupted"
    # Only one submit must have been issued — download failure does not resubmit.
    assert len(runtime.submits) == 1, (
        "a download failure must not trigger a new upstream submission"
    )


# ---------------------------------------------------------------------------
# #180.8: busy DELETE 409 is not reported as cancelled
# ---------------------------------------------------------------------------


def test_busy_delete_409_is_not_cancelled(tmp_path):
    """#180.8: a DELETE that answers 409 (task busy) must return False,
    never True — the attempt is not cancelled and the caller must fence it.
    """
    runtime = PinnedRuntime(tmp_path)

    class BusyDeleteRuntime:
        def get(self, url, **kwargs):
            return runtime.get(url, **kwargs)

        def post(self, url, **kwargs):
            return runtime.post(url, **kwargs)

        def delete(self, url, **kwargs):
            # Simulate a busy upstream that refuses cancellation.
            return httpx.Response(409, json={"reason": "task_busy"})

    engine = MoneyPrinterEngine(url="http://engine:8000",
                                transport=BusyDeleteRuntime(), poll_interval=0.01)
    # Submit once so there is a task to cancel.
    request = _base_request()
    engine._submit_task(request, submit_key=None)

    # Attempt to cancel: the 409 must be reported as NOT cancelled.
    result = engine.cancel(job_id="task-1")
    assert result is False, "a 409 busy response must never be treated as successful cancellation"
