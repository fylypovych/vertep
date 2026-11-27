"""Tests for #550 (i.0.0.5.50): OpenTimelineIO (.otio) timeline export from Storyboard."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import opentimelineio as otio
import pytest

from modules.storyboard import storyboard_module
from modules.storyboard.otio_export import OTIO_MEDIA_TYPE, export_storyboard_otio


def test_otio_export_roundtrip_with_opentimelineio_library():
    storyboard_doc = {
        "job_id": "job-otio-1",
        "brand_id": "brand-a",
        "title": "Історія Карпат",
        "scenes": [
            {
                "scene_id": "scene-001",
                "index": 1,
                "prompt": "Світанок у горах",
                "voiceover": "Починаємо подорож Карпатами.",
                "duration": 4.5,
                "video_url": "file:///data/storage/jobs/job-otio-1/scene-001.mp4",
                "audio_url": "file:///data/storage/jobs/job-otio-1/speech-001.wav",
            },
            {
                "scene_id": "scene-002",
                "index": 2,
                "prompt": "Гірська річка",
                "voiceover": "Вода тече крізь ущелину.",
                "duration": 6.0,
                # No media yet -> must export as MissingReference with explicit metadata
            },
        ],
    }

    raw_otio = export_storyboard_otio(storyboard_doc, fps=25.0, source_kind="job_storyboard")
    timeline = otio.adapters.read_from_string(raw_otio, adapter_name="otio_json")

    assert isinstance(timeline, otio.schema.Timeline)
    assert timeline.name == "Історія Карпат"
    assert timeline.global_start_time.rate == 25.0
    assert len(timeline.tracks) == 2

    video_track = timeline.video_tracks()[0]
    audio_track = timeline.audio_tracks()[0]
    assert len(video_track) == 2
    assert len(audio_track) == 2

    # Scene 1: ExternalReference + exact duration (4.5 * 25 = 112.5 frames) + marker
    clip1 = video_track[0]
    assert clip1.name == "scene-001"
    assert isinstance(clip1.media_reference, otio.schema.ExternalReference)
    assert clip1.media_reference.target_url == "file:///data/storage/jobs/job-otio-1/scene-001.mp4"
    assert clip1.source_range.duration.to_seconds() == pytest.approx(4.5)
    assert len(clip1.markers) == 1
    assert clip1.markers[0].name == "Scene 1: scene-001"

    # Scene 2: MissingReference preserved transparently + exact duration (6.0 s)
    clip2 = video_track[1]
    assert clip2.name == "scene-002"
    assert isinstance(clip2.media_reference, otio.schema.MissingReference)
    assert clip2.media_reference.metadata["vertep"]["missing_media"] is True
    assert clip2.source_range.duration.to_seconds() == pytest.approx(6.0)

    # Audio track matches scene 1 ExternalReference and scene 2 MissingReference
    assert isinstance(audio_track[0].media_reference, otio.schema.ExternalReference)
    assert isinstance(audio_track[1].media_reference, otio.schema.MissingReference)
    assert timeline.duration().to_seconds() == pytest.approx(10.5)


def test_storyboard_http_otio_endpoint_enforces_rbac_and_brand_isolation(monkeypatch):
    sample = {
        "job_id": "job-42",
        "brand_id": "brand-a",
        "character_owner_brands": [],
        "title": "Brand A Storyboard",
        "active_version": 1,
        "storyboards": [
            {"version": 1, "scenes": [
                {"scene_id": "s1", "index": 1, "prompt": "Intro", "duration": 3.0,
                 "media": {"image_url": "/api/jobs/job-42/artifacts/art-photo/download",
                           "audio_url": "/api/jobs/job-42/artifacts/art-speech/download"}},
            ]},
            {"version": 2, "scenes": [{"scene_id": "draft", "duration": 20.0}]},
        ],
    }
    monkeypatch.setattr(storyboard_module, "TOKEN", "test-module-token")
    monkeypatch.setattr(
        storyboard_module,
        "_core",
        lambda path: sample if path == "/api/modules/self/storyboards/job-42" else None,
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), storyboard_module.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        def get(path: str, headers: dict[str, str]):
            req = urllib.request.Request(base + path, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as err:
                return err.code, dict(err.headers), err.read()

        # Without module token -> 403
        status, _, _ = get("/storyboards/job-42/export/otio", {})
        assert status == 403

        # Moderator of another brand -> 404 (brand isolation)
        status, _, _ = get(
            "/storyboards/job-42/export/otio",
            {"x-vertep-module-token": "test-module-token", "x-vertep-role": "moderator", "x-vertep-brands": "brand-b"},
        )
        assert status == 404

        # Moderator of owning brand -> 200 valid .otio
        status, headers, body = get(
            "/storyboards/job-42/export/otio?fps=30",
            {"x-vertep-module-token": "test-module-token", "x-vertep-role": "moderator", "x-vertep-brands": "brand-a"},
        )
        assert status == 200
        assert OTIO_MEDIA_TYPE.split(";")[0] in headers.get("content-type", "")
        assert "storyboard-job-42.otio" in headers.get("content-disposition", "")
        parsed = otio.adapters.read_from_string(body.decode("utf-8"), adapter_name="otio_json")
        assert parsed.global_start_time.rate == 30.0
        assert parsed.duration().to_seconds() == pytest.approx(3.0)
        assert parsed.tracks[0][0].media_reference.target_url.endswith("/art-photo/download")
        assert parsed.tracks[1][0].media_reference.target_url.endswith("/art-speech/download")

        # Invalid fps -> 422
        status, _, _ = get(
            "/storyboards/job-42/export/otio?fps=999",
            {"x-vertep-module-token": "test-module-token", "x-vertep-role": "admin"},
        )
        assert status == 422
    finally:
        server.shutdown()
        server.server_close()


def test_export_uses_media_links_from_core_storyboard_gateway(monkeypatch):
    from types import SimpleNamespace

    from core.artifacts import register_artifact
    from core.models import StoryboardScene, StoryboardVersion
    from core.module import gateway
    from core.state import store

    job = store.create("Storyboard media", "did_samogon", 5)
    image_path = store.root / job.job_id / "images" / "frame.png"
    image_path.write_bytes(b"reference image")
    image = register_artifact(job, store.root, image_path, "storyboard_image")
    audio_path = store.root / job.job_id / "audio" / "speech.wav"
    audio_path.write_bytes(b"reference speech")
    audio = register_artifact(job, store.root, audio_path, "audio", scene_id="active-scene")
    job.storyboards = [
        StoryboardVersion(version=version, title="Title", status="approved", scenes=[scene])
        for version, scene in [
            (1, StoryboardScene(index=1, scene_id="active-scene", prompt="Active",
                                video_prompt="Active", duration=3, image_artifact_id=image.artifact_id)),
            (2, StoryboardScene(index=1, scene_id="draft", prompt="Draft",
                                video_prompt="Draft", duration=20)),
        ]
    ]
    job.active_storyboard_version = 1
    monkeypatch.setenv("PUBLIC_URL", "https://vertep.example")
    monkeypatch.setattr(gateway, "_require_storyboard_module", lambda _: SimpleNamespace(module_id="storyboard"))
    document = gateway.module_storyboard(job.job_id, None)
    timeline = otio.adapters.read_from_string(export_storyboard_otio(document), adapter_name="otio_json")
    assert timeline.duration().to_seconds() == pytest.approx(3)
    for track, artifact in zip(timeline.tracks, (image, audio), strict=True):
        assert track[0].media_reference.target_url == (
            f"https://vertep.example/api/jobs/{job.job_id}/artifacts/{artifact.artifact_id}/download"
        )


def test_export_maps_production_runtime_scene_video_image_and_audio_for_active_version(monkeypatch):
    """Regression (#550): production pipeline stores storyboard scenes as sb-<ver>-<idx>,
    runtime scenes as scene-<idx:03d> with artifact_ids (not StoryboardScene.artifact_id),
    and TTS audio keyed by runtime scene_id."""
    from types import SimpleNamespace

    from core.artifacts import register_artifact
    from core.models import SceneRecord, StoryboardScene, StoryboardVersion
    from core.module import gateway
    from core.state import store

    job = store.create("Production storyboard media", "did_samogon", 5)

    # Scene 1: rendered video + runtime image + TTS audio (all keyed by runtime scene-001)
    img1_path = store.root / job.job_id / "images" / "scene-001.png"
    img1_path.write_bytes(b"png-scene-1")
    img1 = register_artifact(job, store.root, img1_path, "image", scene_id="scene-001")

    vid1_path = store.root / job.job_id / "video" / "scene-001.mp4"
    vid1_path.write_bytes(b"mp4-scene-1")
    vid1 = register_artifact(job, store.root, vid1_path, "video_scene", scene_id="scene-001")

    aud1_path = store.root / job.job_id / "audio" / "voice-scene-001.wav"
    aud1_path.write_bytes(b"wav-scene-1")
    aud1 = register_artifact(job, store.root, aud1_path, "audio", scene_id="scene-001")

    # Scene 2: runtime image only + TTS audio (no video yet, no StoryboardScene.artifact_id)
    img2_path = store.root / job.job_id / "images" / "scene-002.png"
    img2_path.write_bytes(b"png-scene-2")
    img2 = register_artifact(job, store.root, img2_path, "image", scene_id="scene-002")

    aud2_path = store.root / job.job_id / "audio" / "voice-scene-002.wav"
    aud2_path.write_bytes(b"wav-scene-2")
    aud2 = register_artifact(job, store.root, aud2_path, "audio", scene_id="scene-002")

    job.scenes = [
        SceneRecord(scene_id="scene-001", index=1, prompt="S1", duration=4.0,
                    artifact_ids=[img1.artifact_id, vid1.artifact_id]),
        SceneRecord(scene_id="scene-002", index=2, prompt="S2", duration=5.0,
                    artifact_ids=[img2.artifact_id]),
    ]
    job.storyboards = [
        StoryboardVersion(
            version=1,
            title="Active Production Version",
            status="approved",
            scenes=[
                StoryboardScene(index=1, scene_id="sb-1-1", prompt="S1", video_prompt="S1", duration=4),
                StoryboardScene(index=2, scene_id="sb-1-2", prompt="S2", video_prompt="S2", duration=5),
            ],
        ),
        StoryboardVersion(
            version=2,
            title="Inactive Draft Version",
            status="pending_approval",
            scenes=[
                StoryboardScene(index=1, scene_id="sb-2-1", prompt="Draft S1", video_prompt="Draft", duration=9),
            ],
        ),
    ]
    job.active_storyboard_version = 1

    monkeypatch.setenv("PUBLIC_URL", "https://vertep.example")
    monkeypatch.setattr(gateway, "_require_storyboard_module", lambda _: SimpleNamespace(module_id="storyboard"))

    document = gateway.module_storyboard(job.job_id, None)
    active_doc = next(v for v in document["storyboards"] if v["version"] == 1)
    draft_doc = next(v for v in document["storyboards"] if v["version"] == 2)

    # Active version receives video/image/audio URLs from runtime scenes; draft version stays isolated.
    assert active_doc["scenes"][0]["media"]["video_url"].endswith(f"/{vid1.artifact_id}/download")
    assert active_doc["scenes"][0]["media"]["image_url"].endswith(f"/{img1.artifact_id}/download")
    assert active_doc["scenes"][0]["media"]["audio_url"].endswith(f"/{aud1.artifact_id}/download")
    assert active_doc["scenes"][1]["media"]["image_url"].endswith(f"/{img2.artifact_id}/download")
    assert active_doc["scenes"][1]["media"]["audio_url"].endswith(f"/{aud2.artifact_id}/download")
    assert draft_doc["scenes"][0]["media"] == {}

    # OTIO export resolves ExternalReference for both video/image and audio tracks on all active scenes.
    timeline = otio.adapters.read_from_string(export_storyboard_otio(document), adapter_name="otio_json")
    video_track = timeline.video_tracks()[0]
    audio_track = timeline.audio_tracks()[0]
    assert len(video_track) == 2 and len(audio_track) == 2
    assert isinstance(video_track[0].media_reference, otio.schema.ExternalReference)
    assert video_track[0].media_reference.target_url.endswith(f"/{vid1.artifact_id}/download")
    assert isinstance(audio_track[0].media_reference, otio.schema.ExternalReference)
    assert audio_track[0].media_reference.target_url.endswith(f"/{aud1.artifact_id}/download")
    assert isinstance(video_track[1].media_reference, otio.schema.ExternalReference)
    assert video_track[1].media_reference.target_url.endswith(f"/{img2.artifact_id}/download")
    assert isinstance(audio_track[1].media_reference, otio.schema.ExternalReference)
    assert audio_track[1].media_reference.target_url.endswith(f"/{aud2.artifact_id}/download")
