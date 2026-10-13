"""Issue #124 (i.0.0.1.24) — the agreed §9 media matrix through the MPT bridge.

Issue #122 P4 proved one render through the pinned submit route and the pinned
compose helper separately. What stayed unproven is the matrix the owner agreed in
#122 §9.3: every ``image``/``clip``/``audio`` case driven through the standard
``engine → wrapper → pinned submit/status/download`` route, with the registered
artifact **decoded** rather than assumed.

The pinned runtime itself is proven inside its container by
``services/moneyprinter_service.check_submit_route`` (CI:
``scripts/moneyprinter-runtime-check.py``). Here the pinned API is replaced by a
controlled stand-in that follows the documented §9.2 compose semantics, so the
route Vertep owns end to end is exercised for real: executor pre-cut, material
upload, approved-voice staging, submit invariants, status mapping, download,
executor post-step and the verified import. Only the external render is
substituted — everything Vertep is responsible for runs unchanged.

Nothing here adds a format, a provider or a parameter: an input outside the agreed
matrix must be refused with its own reason, never quietly adapted.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
import wave
import zlib
from pathlib import Path

import httpx
import numpy as np
import pytest

from adapters.ffmpeg import FFmpegAdapter
from adapters.providers.base import FIXED_SUBMIT_FIELDS, BridgeContractError
from adapters.providers.video_engines import MONEY_PRINTER_CONTRACT, MoneyPrinterEngine

#: Scene geometry: both edges clear the pinned minimum material edge (470px) and the
#: ratio is the portrait aspect the fixed §9.4 submit set asks for.
SCENE_WIDTH = 486
SCENE_HEIGHT = 864

#: The agreed §9.3 matrix: label, solid colour and the approved scene duration. The
#: durations differ on purpose — a route that ignored them, or reordered the scenes,
#: cannot reproduce this timeline.
MATRIX_SCENES = (
    ("scene-1", (200, 10, 10), 2.0),
    ("scene-2", (10, 200, 10), 3.0),
    ("scene-3", (10, 10, 200), 4.0),
)

#: The approved narration is a tone, not silence and not speech: silence could not tell
#: an approved voice from a missing one, and a TTS fallback would still be "some audio"
#: (§9.6). The approved BGM is a different tone, so a mix that dropped it is audible.
VOICE_FREQUENCY = 440.0
MUSIC_FREQUENCY = 220.0
AUDIO_RATE = 24000

#: Decoded-colour tolerance. Both the expected and the measured colour travel through
#: the same encode path, so a real scene match stays far below this and a substituted
#: or reordered scene does not.
COLOUR_TOLERANCE = 48.0

#: Per-scene duration tolerance of the decoded timeline (§9.3 row 4).
DURATION_TOLERANCE = 0.5


def _png(path: Path, colour: tuple[int, int, int],
         width: int = SCENE_WIDTH, height: int = SCENE_HEIGHT) -> Path:
    """A real PNG of one solid colour, at the geometry the bridge submits."""
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _tone(path: Path, seconds: float, frequency: float,
          rate: int = AUDIO_RATE, amplitude: int = 12000) -> Path:
    """A lossless PCM narration or music track of an identifiable pitch."""
    frames = np.sin(
        2 * np.pi * frequency * np.arange(int(rate * seconds)) / rate
    ) * amplitude
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.astype("<i2").tobytes())
    return path


def _srt(path: Path, seconds: float) -> Path:
    path.write_text(
        "1\n00:00:00,000 --> "
        f"00:00:{int(seconds):02d},000\nЗатверджені субтитри\n\n",
        encoding="utf-8",
    )
    return path


def _ffmpeg() -> str:
    return FFmpegAdapter()._executable()


def _stream_info(path: Path) -> tuple[int, int, float]:
    """Geometry and duration of a media file, read from the decoder itself."""
    completed = subprocess.run(
        [_ffmpeg(), "-nostdin", "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    report = completed.stderr
    duration = 0.0
    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", report)
    if match:
        hours, minutes, seconds = (float(value) for value in match.groups())
        duration = hours * 3600 + minutes * 60 + seconds
    geometry = re.search(r"Video:.*?, (\d+)x(\d+)", report)
    if not geometry:
        raise AssertionError(f"the decoder reported no video stream for {path}")
    return int(geometry.group(1)), int(geometry.group(2)), duration


def _frame(path: Path, at_seconds: float, width: int, height: int) -> np.ndarray:
    """Decode one frame as RGB, so a scene is measured and not assumed."""
    completed = subprocess.run(
        [_ffmpeg(), "-nostdin", "-loglevel", "error", "-ss", f"{at_seconds:.3f}",
         "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:{height},format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    expected = width * height * 3
    if len(completed.stdout) != expected:
        raise AssertionError(
            f"no decodable frame at {at_seconds:.3f}s of {path.name}: "
            f"{len(completed.stdout)} of {expected} bytes"
        )
    return np.frombuffer(completed.stdout, dtype=np.uint8).reshape(height, width, 3)


def _mean_colour(path: Path, at_seconds: float) -> list[float]:
    width, height, _ = _stream_info(path)
    frame = _frame(path, at_seconds, width, height)
    return [float(value) for value in frame.reshape(-1, 3).mean(axis=0)]


def _narration(path: Path, rate: int = 16000) -> np.ndarray:
    completed = subprocess.run(
        [_ffmpeg(), "-nostdin", "-loglevel", "error", "-i", str(path), "-vn",
         "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True,
    )
    if not completed.stdout:
        raise AssertionError(f"no decodable audio track in {path.name}")
    return np.frombuffer(completed.stdout, dtype="<i2").astype(float)


def _peaks(samples: np.ndarray, rate: int = 16000, count: int = 8) -> list[float]:
    """The strongest spectral peaks, so a mixed-in track is audible in the spectrum."""
    spectrum = np.abs(np.fft.rfft(samples))
    frequencies = np.fft.rfftfreq(samples.size, d=1.0 / rate)
    strongest = np.argsort(spectrum)[-count:]
    return sorted(round(float(frequencies[index]), 1) for index in strongest)


def _dominant(samples: np.ndarray, rate: int = 16000) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    frequencies = np.fft.rfftfreq(samples.size, d=1.0 / rate)
    return float(frequencies[int(np.argmax(spectrum))])


def _drift(actual, expected) -> float:
    return max(abs(left - right) for left, right in zip(actual, expected))




# ---------------------------------------------------------------------------
# The pinned API, replaced by a controlled stand-in
# ---------------------------------------------------------------------------


def _runtime_snapshot(**overrides) -> dict:
    body = {
        "service": "vertep-moneyprinter",
        "upstream_commit": MONEY_PRINTER_CONTRACT.upstream_reference.split("@")[1],
        "bridge_schema_version": "v1",
        "capabilities": {"task_local_voice": True, "reason": None},
    }
    body.update(overrides)
    return body


class PinnedRuntime:
    """The pinned MoneyPrinterTurbo API as the bridge addresses it.

    Only the external render is substituted, and it follows the compose semantics
    documented in #122 §9.2: ``video_source="local"`` takes the uploaded materials in
    submit order, one subclip per material in ``sequential``, the total length is the
    approved narration and the frame uses the submitted aspect. The render is therefore
    a real function of what Vertep uploaded, in which order, for how long — a stand-in
    that returned a fixed file could not prove any of that.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "pinned"
        self.materials = self.root / "storage" / "local_videos"
        self.materials.mkdir(parents=True, exist_ok=True)
        self.requests: list[tuple[str, str, dict]] = []
        self.submits: list[dict] = []
        self.without_voice_staging = False
        self.corrupt_download: bytes | None = None
        self._assembly = FFmpegAdapter()

    # -- transport surface the engine uses ---------------------------------

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self._answer("GET", url, kwargs)

    def delete(self, url: str, **kwargs) -> httpx.Response:
        return self._answer("DELETE", url, kwargs)

    # -- routing -----------------------------------------------------------

    def _answer(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        path = url.split(":8000", 1)[-1]
        self.requests.append((method, path, dict(kwargs)))
        if path == "/runtime":
            return httpx.Response(200, json=_runtime_snapshot(
                capabilities={
                    "task_local_voice": not self.without_voice_staging,
                    "reason": ("upstream_voice_staging_unsupported"
                               if self.without_voice_staging else None),
                },
            ))
        if path == "/api/v1/voice":
            return self._stage_voice(kwargs)
        if path == "/api/v1/video_materials":
            return self._upload_material(kwargs)
        if path == "/api/v1/videos":
            return self._submit(kwargs["json"])
        match = re.fullmatch(r"/api/v1/tasks/(.+)", path)
        if match:
            return self._status(match.group(1))
        match = re.fullmatch(r"/api/v1/download/(.+)", path)
        if match:
            return self._download(match.group(1))
        raise AssertionError(f"the pinned runtime was asked for an unknown route: {path}")

    def _stage_voice(self, kwargs: dict) -> httpx.Response:
        payload = kwargs["content"]
        digest = hashlib.sha256(payload).hexdigest()
        name = kwargs["headers"]["x-vertep-filename"]
        self.root.joinpath(f"{digest}{Path(name).suffix}").write_bytes(payload)
        return httpx.Response(200, json={"status": 200, "message": "success", "data": {
            "voice": f"vertep-voice:{digest}{Path(name).suffix}",
            "sha256": digest, "bytes": len(payload),
        }})

    def _upload_material(self, kwargs: dict) -> httpx.Response:
        name = kwargs["headers"]["x-vertep-filename"]
        (self.materials / name).write_bytes(kwargs["content"])
        return httpx.Response(200, json={"status": 200, "message": "success",
                                         "data": {"file": name}})

    def _submit(self, payload: dict) -> httpx.Response:
        self.submits.append(payload)
        return httpx.Response(200, json={"status": 200, "message": "success",
                                         "data": {"task_id": "task-1"}})

    def _status(self, task_id: str) -> httpx.Response:
        if not self.submits:
            return httpx.Response(404, json={"status": 404, "message": "unknown task"})
        return httpx.Response(200, json={"data": {
            "state": 1, "progress": 100,
            "videos": [f"tasks/{task_id}/final-1.mp4"],
        }})

    def _download(self, reference: str) -> httpx.Response:
        """Compose the submitted materials the way the pinned pipeline does (§9.2)."""
        if self.corrupt_download is not None:
            return httpx.Response(200, content=self.corrupt_download,
                                  headers={"content-type": "video/mp4"})
        payload = self.submits[-1]
        marker = str(payload["custom_audio_file"])
        suffix = Path(marker).suffix
        digest = marker[len("vertep-voice:"):-len(suffix)]
        narration = self.root / f"{digest}{suffix}"
        workdir = self.root / "compose"
        workdir.mkdir(parents=True, exist_ok=True)
        clips = []
        for index, material in enumerate(payload["video_materials"]):
            source = self.materials / str(material["url"])
            clip = workdir / f"scene-{index:03d}.mp4"
            if not clip.exists():
                # One subclip per material, in submit order, at its approved duration.
                self._assembly.pre_cut(source, float(material["duration"]), clip)
            clips.append(clip)
        combined = workdir / "combined.mp4"
        self._assembly.assemble_clips(
            combined, clips, audio=narration,
            aspect_ratio=str(payload["video_aspect"]),
        )
        return httpx.Response(200, content=combined.read_bytes(),
                              headers={"content-type": "video/mp4"})



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path):
    return PinnedRuntime(tmp_path)


@pytest.fixture
def engine(runtime):
    return MoneyPrinterEngine(url="http://engine:8000", transport=runtime,
                              poll_interval=0.01)


APPROVED_SCRIPT = "Сцена перша.\nСцена друга.\nСцена третя."


def _approved_assets(tmp_path: Path, *, scenes=MATRIX_SCENES, with_music=False,
                     with_subtitles=False, with_watermark=False) -> dict:
    """The approved Job inputs of one attempt, exactly as ``finalize_job`` passes them."""
    root = tmp_path / "job"
    root.mkdir(parents=True, exist_ok=True)
    total = sum(seconds for _, _, seconds in scenes)
    return {
        "images": [_png(root / f"{label}.png", colour) for label, colour, _ in scenes],
        "clips": None,
        "durations": [seconds for _, _, seconds in scenes],
        "audio": _tone(root / "voice.wav", total, VOICE_FREQUENCY),
        "music": _tone(root / "music.wav", total, MUSIC_FREQUENCY) if with_music else None,
        "subtitles": _srt(root / "voice.srt", total) if with_subtitles else None,
        "watermark": _png(root / "logo.png", (255, 255, 255), 160, 160)
                     if with_watermark else None,
        "aspect_ratio": "9:16",
        "preset": None,
        "task_type": "image",
        "script": APPROVED_SCRIPT,
    }


def _timeline(artifact: Path, scenes=MATRIX_SCENES) -> list[dict]:
    """Decode the artifact at the midpoint of every approved scene (§9.3 rows 2–4)."""
    width, height, _ = _stream_info(artifact)
    measured = []
    offset = 0.0
    for label, _, seconds in scenes:
        frame = _frame(artifact, offset + seconds / 2, width, height)
        measured.append({
            "label": label,
            "seconds": seconds,
            "at_seconds": round(offset + seconds / 2, 3),
            "mean_colour": [float(value) for value in frame.reshape(-1, 3).mean(axis=0)],
        })
        offset += seconds
    return measured


def _assert_scene_matrix(artifact: Path, scenes=MATRIX_SCENES) -> None:
    """Fail unless the artifact shows every approved scene, in order, at its duration.

    The comparison is deliberately scale-invariant: a preset other than the job aspect
    pads the frame, so the same scene measures darker than it did in the material. What
    cannot survive padding is *which* scene is in *which* slot, so every slot has to be
    nearer to its own approved colour than to any other approved one, and no two slots
    may measure alike — a blank frame, a dropped scene or a reordered compose fails both.
    """
    width, height, duration = _stream_info(artifact)
    assert width > 0 and height > 0, "the registered artifact carries no video stream"
    expected_total = sum(seconds for _, _, seconds in scenes)
    assert duration == pytest.approx(expected_total, abs=DURATION_TOLERANCE), (
        f"registered artifact runs {duration:.2f}s for {expected_total:.2f}s of "
        f"approved scenes"
    )
    timeline = _timeline(artifact, scenes)
    assert [entry["label"] for entry in timeline] == [label for label, _, _ in scenes]

    approved = [[float(value) for value in colour] for _, colour, _ in scenes]
    for index, entry in enumerate(timeline):
        measured = entry["mean_colour"]
        distances = [_drift(measured, colour) for colour in approved]
        assert distances.index(min(distances)) == index, (
            f"{entry['label']} at {entry['at_seconds']}s is nearest to "
            f"{scenes[distances.index(min(distances))][0]}, not to its own approved scene: "
            f"rendered {measured}"
        )
        # Padding can only darken a frame, never brighten it past the approved colour, so
        # a scene rendered brighter than it was approved for is a substitution.
        for channel, expected in enumerate(approved[index]):
            assert measured[channel] <= expected + COLOUR_TOLERANCE, (
                f"{entry['label']} at {entry['at_seconds']}s is brighter than the approved "
                f"scene in channel {channel}: {measured} against {approved[index]}"
            )
    for first, second in zip(timeline, timeline[1:]):
        assert _drift(first["mean_colour"], second["mean_colour"]) > COLOUR_TOLERANCE, (
            f"{first['label']} and {second['label']} render alike at their approved "
            f"timestamps, so the timeline proves nothing about the scene order"
        )
    assert timeline[-1]["at_seconds"] < duration, (
        "the approved timeline runs past the registered artifact"
    )


def _assert_submit_contract(runtime: PinnedRuntime) -> dict:
    """One submit, carrying the whole fixed §9.4 set and nothing outside it."""
    assert len(runtime.submits) == 1, (
        f"one attempt produced {len(runtime.submits)} upstream tasks"
    )
    payload = runtime.submits[0]
    assert set(payload) == set(FIXED_SUBMIT_FIELDS), (
        "the submit set drifted from the pinned §9.4 contract"
    )
    assert payload["video_source"] == "local"
    assert payload["subtitle_enabled"] is False, (
        "the pinned runtime must not run its own subtitle stage"
    )
    assert not payload["bgm_type"] and not payload["bgm_file"], (
        "the approved BGM belongs to the executor post-step, not to the runtime"
    )
    assert payload["video_count"] == 1
    return payload


# ---------------------------------------------------------------------------
# §9.3 rows 2–4 and 8: scene order, scene durations and aspect, decoded
# ---------------------------------------------------------------------------


def test_the_agreed_scene_matrix_is_rendered_and_registered(engine, runtime, tmp_path):
    """Issue #124: every agreed image case goes through the real bridge route.

    §9.3 rows 2–4 are the executor's duty: one pre-cut clip per approved scene, in
    scene order, each carrying its exact approved duration, composed at the job aspect.
    A runtime that reordered, re-timed or dropped a scene changes the colours at the
    expected timestamps, so the registered artifact is decoded and measured instead of
    being accepted because a file appeared.
    """
    assets = _approved_assets(tmp_path)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    payload = _assert_submit_contract(runtime)
    assert [material["duration"] for material in payload["video_materials"]] == \
        assets["durations"], "a scene reached the runtime without its approved duration"
    assert payload["video_aspect"] == assets["aspect_ratio"]
    assert payload["custom_audio_file"].startswith("vertep-voice:")

    _assert_scene_matrix(artifact, )
    # §9.3 row 8: the post-step applies the job aspect, so the registered artifact is
    # geometrically what the Job asked for.
    width, height, _ = _stream_info(artifact)
    assert round(height / width, 2) == round(16 / 9, 2), (
        f"the registered artifact ignored the job aspect: {width}x{height}"
    )


def test_the_scene_order_is_proven_not_assumed(engine, runtime, tmp_path):
    """A reordered compose must be visible in the decoded timeline, not just claimed.

    Each scene is a distinct solid colour, so a stand-in that composed the uploaded
    clips in a different order produces a different colour at each timestamp. Every slot
    has to be nearest to its own approved scene, so a passing run is evidence about the
    order rather than about the presence of three clips.
    """
    assets = _approved_assets(tmp_path)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    approved = [[float(value) for value in colour] for _, colour, _ in MATRIX_SCENES]
    timeline = _timeline(artifact)
    assert [entry["label"] for entry in timeline] == [label for label, _, _ in MATRIX_SCENES]
    for index, entry in enumerate(timeline):
        distances = [_drift(entry["mean_colour"], colour) for colour in approved]
        assert distances.index(min(distances)) == index, (
            f"{entry['label']} at {entry['at_seconds']}s shows a different scene than the "
            f"one approved for that slot: rendered {entry['mean_colour']}"
        )


@pytest.mark.parametrize("preset,expected", [
    ("youtube", (1280, 720, 25)),
    ("tiktok", (720, 1280, 30)),
    ("square", (1080, 1080, 25)),
])
def test_every_agreed_preset_reaches_the_registered_artifact(
    engine, runtime, tmp_path, preset, expected
):
    """§9.3 row 9: the preset has no upstream parameter, so the post-step must apply it.

    The runtime receives no preset at all; the geometry and frame rate of the
    registered artifact are therefore pure proof that the executor post-step ran with
    the approved ``PRESETS`` entry.
    """
    assets = _approved_assets(tmp_path)
    assets["preset"] = preset
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    width, height, _ = _stream_info(artifact)
    assert (width, height) == (expected[0], expected[1]), (
        f"preset {preset} did not reach the registered artifact: {width}x{height}"
    )
    payload = _assert_submit_contract(runtime)
    assert "preset" not in payload and "output_preset" not in payload, (
        "the pinned submit set must not grow a parameter the runtime does not have"
    )
    _assert_scene_matrix(artifact, )


def test_the_agreed_clip_matrix_is_rendered_and_registered(engine, runtime, tmp_path):
    """§9.3 row 3: the ``video`` task type travels the same route as ``image``.

    Approved scene clips are pre-cut to their durations and uploaded like stills, so
    the decoded timeline has to match just the same — including that no watermark is
    applied for a clip task (§9.3 row 10).
    """
    assembly = FFmpegAdapter()
    root = tmp_path / "job"
    root.mkdir(parents=True, exist_ok=True)
    total = sum(seconds for _, _, seconds in MATRIX_SCENES)
    clips = []
    for index, (label, colour, seconds) in enumerate(MATRIX_SCENES):
        still = _png(root / f"{label}.png", colour)
        clips.append(assembly.pre_cut(still, seconds, root / f"{label}.mp4"))
    assets = _approved_assets(tmp_path, with_watermark=True)
    assets.update(images=None, clips=clips, task_type="video")
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    payload = _assert_submit_contract(runtime)
    # Every approved scene travels as its own pre-cut clip, in scene order. The executor
    # names the cut clips itself, so the identity is the count and the order, not the
    # approved file name.
    assert len(payload["video_materials"]) == len(clips)
    assert [material["duration"] for material in payload["video_materials"]] == \
        assets["durations"]
    _assert_scene_matrix(artifact)

    # A clip task must not be watermarked: ``assemble_clips`` never was, and adding one
    # would silently change what the Job approved.
    watermarked = engine.render(tmp_path / "final" / "video-v2.mp4", **dict(
        assets, watermark=_png(root / "logo.png", (255, 255, 255), 160, 160),
    ))
    width, height, _ = _stream_info(watermarked)
    reference = _frame(watermarked, total / 2, width, height)
    unstamped = engine.render(tmp_path / "final" / "video-v3.mp4", **dict(
        assets, watermark=None,
    ))
    clean = _frame(unstamped, total / 2, width, height)
    assert float(np.abs(reference[-200:, -200:].astype(float)
                        - clean[-200:, -200:].astype(float)).mean()) < 1.0, (
        "a clip task was watermarked, which the approved contract does not allow"
    )


# ---------------------------------------------------------------------------
# §9.3 rows 5–7 and 10: approved audio, SRT, BGM and watermark, decoded
# ---------------------------------------------------------------------------


def test_the_approved_voice_is_the_one_in_the_registered_artifact(engine, runtime,
                                                                 tmp_path):
    """§9.3 row 5: the narration of the artifact is the approved voice, measured.

    The approved narration is a tone of an identifiable pitch. A TTS fallback, a missing
    track or a silent mix all produce "some audio", so the registered artifact's track is
    decoded and its dominant frequency compared with the approved one (§9.6).
    """
    assets = _approved_assets(tmp_path)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    payload = _assert_submit_contract(runtime)
    assert payload["custom_audio_file"].startswith("vertep-voice:"), (
        "the runtime must receive a staged content address, never a CORE path"
    )
    samples = _narration(artifact)
    assert _dominant(samples) == pytest.approx(VOICE_FREQUENCY, rel=0.02), (
        f"the registered artifact is not narrated by the approved voice: dominant "
        f"{_dominant(samples):.0f}Hz against the approved {VOICE_FREQUENCY:.0f}Hz"
    )
    assert float(np.sqrt(np.mean(np.square(samples))) / 32768.0) > 0.02, (
        "the narration of the registered artifact is inaudible"
    )


def test_the_approved_bgm_is_mixed_only_by_the_executor(engine, runtime, tmp_path):
    """§9.3 row 6: the runtime never receives the BGM; the post-step mixes it.

    ``bgm_type``/``bgm_file`` stay empty in the submit, so the music cannot be fetched or
    played by the runtime. The proof that the approved BGM really reached the artifact is
    spectral: its own pitch is present next to the approved narration.
    """
    assets = _approved_assets(tmp_path, with_music=True)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    payload = _assert_submit_contract(runtime)
    assert payload["bgm_volume"] == 0.0
    peaks = _peaks(_narration(artifact))
    assert any(abs(peak - MUSIC_FREQUENCY) <= 5 for peak in peaks), (
        f"the approved BGM is missing from the registered artifact: peaks {peaks}"
    )
    assert _dominant(_narration(artifact)) == pytest.approx(VOICE_FREQUENCY, rel=0.02), (
        "the narration must stay the dominant track, with the BGM underneath it"
    )


def test_the_approved_srt_is_burned_only_under_the_agreed_policy(engine, runtime,
                                                                 tmp_path, monkeypatch):
    """§9.3 row 7: the approved SRT is burned exactly when Native would burn it.

    The runtime cannot take a ready SRT, so the burn is the executor's. The policy is the
    same one ``FFmpegAdapter`` uses, which means the approved subtitles are burned with
    ``BURN_SUBTITLES=true`` and left alone otherwise — never silently modified.
    """
    assets = _approved_assets(tmp_path, with_subtitles=True)
    burned = tmp_path / "final" / "video-v1.mp4"

    monkeypatch.setenv("BURN_SUBTITLES", "true")
    engine.render(burned, **assets)
    payload = _assert_submit_contract(runtime)
    assert payload["subtitle_enabled"] is False, (
        "the runtime must not run its own subtitle stage, approved SRT or not"
    )
    width, height, _ = _stream_info(burned)
    stamped = _frame(burned, assets["durations"][0] / 2, width, height)

    monkeypatch.setenv("BURN_SUBTITLES", "false")
    engine.render(tmp_path / "final" / "video-v2.mp4", **assets)
    plain = _frame(tmp_path / "final" / "video-v2.mp4",
                   assets["durations"][0] / 2, width, height)

    # The subtitles live in the lower band of the frame, so that is where the policy has
    # to be visible — and where its absence has to be visible too.
    band = slice(-max(80, height // 5), None)
    assert float(np.abs(stamped[band].astype(float)
                        - plain[band].astype(float)).mean()) > 1.0, (
        "BURN_SUBTITLES=true did not burn the approved subtitles"
    )
    assert float(np.abs(stamped[:height // 4].astype(float)
                        - plain[:height // 4].astype(float)).mean()) < 1.0, (
        "the burn changed pixels outside the subtitle band"
    )


def test_the_watermark_is_overlaid_for_an_image_task(engine, runtime, tmp_path):
    """§9.3 row 10: the watermark overlay belongs to the executor post-step."""
    stamped = _approved_assets(tmp_path, with_watermark=True)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **stamped)
    _assert_submit_contract(runtime)

    clean = dict(stamped, watermark=None)
    plain = engine.render(tmp_path / "final" / "video-v2.mp4", **clean)

    width, height, _ = _stream_info(artifact)
    overlay = _frame(artifact, stamped["durations"][0] / 2, width, height)
    untouched = _frame(plain, stamped["durations"][0] / 2, width, height)
    assert float(np.abs(overlay[-200:, -200:].astype(float)
                        - untouched[-200:, -200:].astype(float)).mean()) > 1.0, (
        "the approved watermark never reached the registered artifact"
    )
    _assert_scene_matrix(artifact)



# ---------------------------------------------------------------------------
# §9.3 rows outside the matrix: explicit refusal, never quiet adaptation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value,code", [
    ("aspect_ratio", "1:1", "unsupported_aspect"),
    ("aspect_ratio", "4:3", "unsupported_aspect"),
    ("preset", "cinema-4k", "preset_unknown"),
    ("task_type", "audio", "unsupported_task_type"),
    ("audio", None, "missing_voice_audio"),
    ("durations", [1.0], "duration_count_mismatch"),
    ("durations", [0.0, 3.0, 4.0], "invalid_duration"),
    ("durations", [-2.0, 3.0, 4.0], "invalid_duration"),
    ("durations", [16.0, 3.0, 4.0], "duration_exceeds_clip_limit"),
    ("durations", [12.0, 3.0, 4.0], "duration_mismatch"),
    ("music", Path("missing.wav"), "music_unusable"),
    ("subtitles", Path("missing.srt"), "subtitle_unusable"),
    ("watermark", Path("missing.png"), "watermark_unusable"),
])
def test_a_parameter_outside_the_agreed_matrix_is_refused(engine, runtime, tmp_path,
                                                          field, value, code):
    """Issue #124: no new format, provider or adaptation — an explicit reason instead.

    §9.9 requires every out-of-matrix input to be refused before dispatch, with the code
    that names its own cause. Nothing may be adapted, rounded, dropped or substituted,
    so the refusal must happen with no upstream contact at all.
    """
    assets = _approved_assets(tmp_path)
    assets[field] = value

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert refusal.value.code == code
    assert runtime.requests == [], (
        f"a refused attempt still contacted the runtime: {runtime.requests}"
    )
    assert not (tmp_path / "final" / "video-v1.mp4").exists(), (
        "a refused attempt registered an artifact"
    )


def test_an_undersized_scene_is_refused(engine, runtime, tmp_path):
    """§9.3 row 2: a material the runtime would reject proves nothing about the route.

    The pinned preprocessor fails the whole task at the ``materials`` stage for a local
    clip below its minimum edge, so such a scene has to be refused here, where the
    failure can still name its own cause.
    """
    assets = _approved_assets(tmp_path)
    assets["images"] = [
        assets["images"][0],
        _png(tmp_path / "small.png", (10, 200, 10), 64, 64),
        assets["images"][2],
    ]

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert refusal.value.code == "asset_below_min_resolution"
    assert runtime.requests == []


def test_a_mixed_media_job_is_refused(engine, runtime, tmp_path):
    """§9.3 row 11: one task type per attempt; images and clips are not interchangeable."""
    assets = _approved_assets(tmp_path)
    assets["clips"] = list(assets["images"])

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert refusal.value.code == "material_channel_mismatch"
    assert runtime.requests == []


def test_an_unwritable_output_is_refused(engine, runtime, tmp_path):
    """§9.3 row 1: an attempt that cannot register its artifact must not be rendered."""
    assets = _approved_assets(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")



# ---------------------------------------------------------------------------
# §9.6: the stages the runtime may not run, and the boundary it may not cross
# ---------------------------------------------------------------------------


def test_the_runtime_is_never_asked_to_generate_or_publish(engine, runtime, tmp_path):
    """Issue #124: script, narration, subtitles, BGM and publication stay with Vertep.

    Every route the attempt touches is recorded, so the whole flow is the evidence: the
    narration is staged, each scene is uploaded, the approved script is submitted, the
    status is polled and the result is downloaded. A runtime that reached for its own
    narration, script or publication capability would show up as a route nobody agreed.
    """
    assets = _approved_assets(tmp_path, with_music=True, with_subtitles=True,
                              with_watermark=True)
    engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    used = sorted({path for _, path, _ in runtime.requests})
    assert used, "the attempt never reached the runtime, so nothing was proven"
    assert used == sorted({
        "/runtime",
        "/api/v1/video_materials",
        "/api/v1/voice",
        "/api/v1/videos",
        "/api/v1/tasks/task-1",
        "/api/v1/download/task-1/final-1.mp4",
    }), f"the attempt used a route outside the agreed set: {used}"

    payload = _assert_submit_contract(runtime)
    assert payload["video_script"] == assets["script"], (
        "the runtime must receive the approved narration text, not generate its own"
    )
    assert payload["video_terms"] is None, "no term generation may be requested"


def test_only_local_staged_materials_reach_the_runtime(engine, runtime, tmp_path):
    """§9.6: local-only. Every material is a storage key the runtime already holds."""
    assets = _approved_assets(tmp_path)
    engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    payload = _assert_submit_contract(runtime)
    for material in payload["video_materials"]:
        reference = str(material["url"])
        assert "://" not in reference and "/" not in reference and "\\" not in reference, (
            f"a scene material was addressed by something other than a storage key: "
            f"{reference}"
        )
        assert material["provider"] == "local"
    # The uploaded bytes are the pre-cut clips, so the runtime cannot fetch anything else.
    uploads = [kwargs["content"] for method, path, kwargs in runtime.requests
               if method == "POST" and path == "/api/v1/video_materials"]
    assert len(uploads) == len(assets["images"])
    assert all(uploaded for uploaded in uploads)


def test_a_runtime_without_voice_staging_is_refused_before_dispatch(engine, runtime,
                                                                   tmp_path):
    """§9.6: a runtime that would have to narrate on its own is refused, not accepted."""
    assets = _approved_assets(tmp_path)
    runtime.without_voice_staging = True

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert refusal.value.code == "upstream_voice_staging_unsupported"
    assert [path for _, path, _ in runtime.requests] == ["/runtime"], (
        "a runtime that cannot take the approved voice was contacted further"
    )


# ---------------------------------------------------------------------------
# §9.5 step 5: the registered artifact is verified, not merely present
# ---------------------------------------------------------------------------


def test_the_registered_artifact_is_checksummed_sized_and_decodable(engine, runtime,
                                                                    tmp_path):
    """Issue #124: output checksum, size and media are all verified before registration."""
    assets = _approved_assets(tmp_path)
    artifact = engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert artifact.is_file() and artifact.stat().st_size > 0
    sidecar = artifact.with_suffix(artifact.suffix + ".sha256")
    assert sidecar.is_file(), "the registered artifact carries no checksum sidecar"
    assert sidecar.read_text(encoding="utf-8") == hashlib.sha256(
        artifact.read_bytes()).hexdigest(), "the recorded checksum does not match the file"
    # Decodable in place, at the approved total length.
    width, height, duration = _stream_info(artifact)
    assert width > 0 and height > 0
    assert duration == pytest.approx(sum(assets["durations"]), abs=DURATION_TOLERANCE)
    # Nothing partial or temporary survives a successful attempt.
    final = tmp_path / "final"
    assert not list(final.glob("*.tmp"))
    assert not list(final.glob("*-poststep.mp4"))
    assert not list(final.glob(".*-bridge")), "the attempt staging area was left behind"


def test_a_corrupted_download_is_never_registered(engine, runtime, tmp_path):
    """A 200 with unusable bytes must not become the Job artifact (§9.5)."""
    assets = _approved_assets(tmp_path)
    runtime.corrupt_download = b"not-a-video"

    with pytest.raises(BridgeContractError) as refusal:
        engine.render(tmp_path / "final" / "video-v1.mp4", **assets)

    assert refusal.value.code == "upstream_result_corrupted"
    assert not (tmp_path / "final" / "video-v1.mp4").exists()
    assert not list((tmp_path / "final").glob("*-poststep.mp4"))

