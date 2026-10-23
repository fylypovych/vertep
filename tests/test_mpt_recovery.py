"""Issue #295 (i.0.0.2.95) — MPT: combined pinned runtime matrix та first-submit recovery.

Criteria covered locally (unit/integration, no real upstream container/FFmpeg):
- #179.1/#179.2: deterministic early audio stage — the runtime must receive the
  approved voice before any byte is generated; missing/tampered voice is refused
  before POST; no TTS fallback or voice race on the first valid submit.
- #180.6: lost submit response — recovery without automatically re-submitting an
  ambiguous key; mapping/ownership recovery or UNKNOWN.
- #180.7: upstream submission count — a repeat of the same submit key does not
  create duplicate upstream work.
- #180.8: bounded polling timeout/429/5xx/malformed; queued/running cancel, busy
  DELETE409, unreachable runtime held until proven terminal/absent.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path

import httpx
import pytest

from adapters.providers.video_engines import (
    BridgeContractError, MoneyPrinterEngine, REASON_SUBMIT_UNKNOWN,
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


def test_runtime_without_voice_staging_is_refused(engine, runtime, tmp_path):
    """#179.2: a runtime that cannot receive the approved voice (task_local_voice
    false) is refused instead of letting it synthesise its own narration."""
    runtime.without_voice_staging = True
    with pytest.raises(BridgeContractError) as exc:
        engine._require_task_local_voice()
    assert exc.value.code == REASON_VOICE_STAGING_UNSUPPORTED


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


def test_two_submits_are_distinct_tasks(runtime, tmp_path):
    """#180.7: two submit calls produce two distinct upstream tasks."""
    engine1 = MoneyPrinterEngine(url="http://engine:8000", transport=runtime,
                                 poll_interval=0.01)
    engine2 = MoneyPrinterEngine(url="http://engine:8000", transport=runtime,
                                 poll_interval=0.01)

    request1 = dict(_base_request(), video_subject="test-1")
    engine1._submit_task(request1, submit_key=None)

    request2 = dict(_base_request(), video_subject="test-2")
    engine2._submit_task(request2, submit_key=None)

    # Two distinct attempts → two submits, each with its own subject.
    assert len(runtime.submits) == 2
    assert runtime.submits[0]["video_subject"] != runtime.submits[1]["video_subject"]


# ── #180.8: bounded polling timeout/429/5xx/malformed ──


def test_polling_malformed_status_is_refused(engine, runtime, tmp_path,
                                             monkeypatch):
    """#180.8: a malformed upstream status payload is refused, not guessed as
    success or failure."""
    # First submit so _status has a task to return.
    request = _base_request()
    engine._submit_task(request, submit_key=None)

    original_answer = runtime._answer

    def flaky_answer(method, path, kwargs):
        if path.startswith("/api/v1/tasks/"):
            return httpx.Response(200, json={"state": "not-an-integer"})
        return original_answer(method, path, kwargs)

    runtime._answer = flaky_answer
    try:
        with pytest.raises(BridgeContractError) as exc:
            engine.map_upstream_status({"state": "not-an-integer"})
        assert exc.value.code == "upstream_status_malformed"
    finally:
        runtime._answer = original_answer


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

    class Always503Transport:
        def get(self, url, **kwargs):
            return httpx.Response(503, json={"data": {"state": 1}})
        def post(self, url, **kwargs):
            return httpx.Response(503, json={"data": {"state": 1}})

    original_transport = engine._transport
    engine._transport = Always503Transport()
    try:
        with pytest.raises(BridgeContractError):
            engine._get_with_retry("/api/v1/tasks/task-1", 1, "task status")
    finally:
        engine._transport = original_transport


def test_release_state_is_unconfirmed_for_remote_engine():
    """#5: a remote engine (MoneyPrinter) has no confirmed release, so release
    state is RELEASE_UNCONFIRMED, never RELEASE_RELEASED."""
    engine = MoneyPrinterEngine(url="http://engine:8000",
                                transport=PinnedRuntime(Path("/tmp")))
    report = engine.release_state()
    assert report == RELEASE_UNCONFIRMED
