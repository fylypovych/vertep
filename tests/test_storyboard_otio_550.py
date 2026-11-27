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
        "scenes": [
            {"scene_id": "s1", "index": 1, "prompt": "Intro", "duration": 3.0, "video_url": "/media/s1.mp4"},
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

        # Invalid fps -> 422
        status, _, _ = get(
            "/storyboards/job-42/export/otio?fps=999",
            {"x-vertep-module-token": "test-module-token", "x-vertep-role": "admin"},
        )
        assert status == 422
    finally:
        server.shutdown()
        server.server_close()
