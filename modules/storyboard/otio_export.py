"""OpenTimelineIO (.otio) timeline exporter for Vertep Storyboard (#550, Apache-2.0).

Converts a Job Storyboard or Scenario Library document/version into a valid
OpenTimelineIO ``Timeline.1`` JSON payload with separate Video and Audio tracks,
accurate RationalTime scene timings, scene markers, and explicit
ExternalReference / MissingReference entries for scene media artifacts.

Does not alter or replace ``FFmpegAdapter`` or the standard Worker ``assembly``
pipeline.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_FPS = 24.0
OTIO_MEDIA_TYPE = "application/vnd.opentimelineio+json; charset=utf-8"


def _extract_scenes(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the selected Storyboard version or the supplied Scenario scenes."""
    sources = [document]
    for key in ("storyboards", "versions"):
        versions = document.get(key)
        if not isinstance(versions, list) or not versions:
            continue
        active = document.get("active_version")
        selected = (versions[-1] if active is None else
                    next((item for item in versions
                          if isinstance(item, dict) and item.get("version") == active), None))
        if selected is None:
            raise ValueError("Active storyboard version is unavailable")
        sources.insert(0, selected)
        break
    sources.append(document.get("script"))
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("scenes"), list):
            return [scene for scene in source["scenes"] if isinstance(scene, dict)]
    return []


def _scene_duration_seconds(scene: dict[str, Any]) -> float:
    raw = scene.get("duration")
    try:
        value = float(raw) if raw is not None else 5.0
    except (TypeError, ValueError):
        value = 5.0
    return max(0.1, value)


def _media_url(scene: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    media = scene.get("media")
    if isinstance(media, dict):
        for key in keys:
            value = media.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _build_with_otio_library(
    title: str,
    scenes: list[dict[str, Any]],
    fps: float,
    metadata: dict[str, Any],
) -> str:
    import opentimelineio as otio

    timeline = otio.schema.Timeline(name=title, metadata={"vertep": metadata})
    timeline.global_start_time = otio.opentime.RationalTime(0, fps)

    video_track = otio.schema.Track(name="Video 1", kind=otio.schema.TrackKind.Video)
    audio_track = otio.schema.Track(name="Audio 1", kind=otio.schema.TrackKind.Audio)

    cursor_frames = 0.0
    for idx, scene in enumerate(scenes, 1):
        scene_id = str(scene.get("scene_id") or f"scene-{idx:03d}")
        duration_s = _scene_duration_seconds(scene)
        frames = round(duration_s * fps, 3)
        time_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, fps),
            duration=otio.opentime.RationalTime(frames, fps),
        )
        scene_meta = {
            "scene_id": scene_id,
            "index": int(scene.get("index") or idx),
            "prompt": str(scene.get("prompt") or ""),
            "video_prompt": str(scene.get("video_prompt") or ""),
            "voiceover": str(scene.get("voiceover") or scene.get("dialogue") or ""),
            "duration_seconds": duration_s,
        }

        video_target = _media_url(scene, ("video_url", "video_path", "image_url", "image_path", "media_url", "artifact_path"))
        if video_target:
            video_ref = otio.schema.ExternalReference(target_url=video_target, available_range=time_range)
        else:
            video_ref = otio.schema.MissingReference(
                name=f"missing-video-{scene_id}",
                metadata={"vertep": {"missing_media": True, "kind": "video", "scene_id": scene_id}},
            )
        video_clip = otio.schema.Clip(
            name=scene_id,
            media_reference=video_ref,
            source_range=time_range,
            metadata={"vertep": scene_meta},
        )
        video_clip.markers.append(
            otio.schema.Marker(
                name=f"Scene {idx}: {scene_id}",
                marked_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, fps),
                    duration=otio.opentime.RationalTime(0, fps),
                ),
                color=otio.schema.MarkerColor.GREEN if video_target else otio.schema.MarkerColor.RED,
                metadata={"vertep": scene_meta},
            )
        )
        video_track.append(video_clip)

        audio_target = _media_url(scene, ("audio_url", "audio_path", "voice_url", "voice_path"))
        if audio_target:
            audio_ref = otio.schema.ExternalReference(target_url=audio_target, available_range=time_range)
        else:
            audio_ref = otio.schema.MissingReference(
                name=f"missing-audio-{scene_id}",
                metadata={"vertep": {"missing_media": True, "kind": "audio", "scene_id": scene_id}},
            )
        audio_clip = otio.schema.Clip(
            name=f"{scene_id}-audio",
            media_reference=audio_ref,
            source_range=time_range,
            metadata={"vertep": {"scene_id": scene_id, "voiceover": scene_meta["voiceover"]}},
        )
        audio_track.append(audio_clip)
        cursor_frames += frames

    timeline.tracks.append(video_track)
    timeline.tracks.append(audio_track)
    return otio.adapters.write_to_string(timeline, adapter_name="otio_json")


def _build_pure_json_otio(
    title: str,
    scenes: list[dict[str, Any]],
    fps: float,
    metadata: dict[str, Any],
) -> str:
    """Spec-compliant OTIO JSON fallback if opentimelineio is unavailable."""
    def rtime(value: float) -> dict:
        return {"OTIO_SCHEMA": "RationalTime.1", "rate": fps, "value": value}

    def trange(start: float, duration: float) -> dict:
        return {"OTIO_SCHEMA": "TimeRange.1", "start_time": rtime(start), "duration": rtime(duration)}

    video_clips: list[dict] = []
    audio_clips: list[dict] = []
    for idx, scene in enumerate(scenes, 1):
        scene_id = str(scene.get("scene_id") or f"scene-{idx:03d}")
        duration_s = _scene_duration_seconds(scene)
        frames = round(duration_s * fps, 3)
        sr = trange(0.0, frames)
        scene_meta = {
            "scene_id": scene_id,
            "index": int(scene.get("index") or idx),
            "prompt": str(scene.get("prompt") or ""),
            "video_prompt": str(scene.get("video_prompt") or ""),
            "voiceover": str(scene.get("voiceover") or scene.get("dialogue") or ""),
            "duration_seconds": duration_s,
        }
        video_target = _media_url(scene, ("video_url", "video_path", "image_url", "image_path", "media_url", "artifact_path"))
        video_ref = (
            {"OTIO_SCHEMA": "ExternalReference.1", "metadata": {}, "name": "", "target_url": video_target, "available_range": sr}
            if video_target
            else {"OTIO_SCHEMA": "MissingReference.1", "metadata": {"vertep": {"missing_media": True, "kind": "video", "scene_id": scene_id}}, "name": f"missing-video-{scene_id}"}
        )
        video_clips.append({
            "OTIO_SCHEMA": "Clip.1",
            "name": scene_id,
            "metadata": {"vertep": scene_meta},
            "source_range": sr,
            "media_references": {"DEFAULT_MEDIA": video_ref},
            "active_media_reference_key": "DEFAULT_MEDIA",
            "markers": [{
                "OTIO_SCHEMA": "Marker.2",
                "name": f"Scene {idx}: {scene_id}",
                "color": "GREEN" if video_target else "RED",
                "marked_range": trange(0.0, 0.0),
                "metadata": {"vertep": scene_meta},
            }],
            "effects": [],
        })
        audio_target = _media_url(scene, ("audio_url", "audio_path", "voice_url", "voice_path"))
        audio_ref = (
            {"OTIO_SCHEMA": "ExternalReference.1", "metadata": {}, "name": "", "target_url": audio_target, "available_range": sr}
            if audio_target
            else {"OTIO_SCHEMA": "MissingReference.1", "metadata": {"vertep": {"missing_media": True, "kind": "audio", "scene_id": scene_id}}, "name": f"missing-audio-{scene_id}"}
        )
        audio_clips.append({
            "OTIO_SCHEMA": "Clip.1",
            "name": f"{scene_id}-audio",
            "metadata": {"vertep": {"scene_id": scene_id, "voiceover": scene_meta["voiceover"]}},
            "source_range": sr,
            "media_references": {"DEFAULT_MEDIA": audio_ref},
            "active_media_reference_key": "DEFAULT_MEDIA",
            "markers": [],
            "effects": [],
        })

    doc = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": title,
        "global_start_time": rtime(0.0),
        "metadata": {"vertep": metadata},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "metadata": {},
            "children": [
                {"OTIO_SCHEMA": "Track.1", "name": "Video 1", "kind": "Video", "metadata": {}, "children": video_clips, "markers": [], "effects": []},
                {"OTIO_SCHEMA": "Track.1", "name": "Audio 1", "kind": "Audio", "metadata": {}, "children": audio_clips, "markers": [], "effects": []},
            ],
            "markers": [],
            "effects": [],
        },
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def export_storyboard_otio(
    document: dict[str, Any],
    *,
    fps: float = DEFAULT_FPS,
    source_kind: str = "storyboard",
) -> str:
    """Serialize a Storyboard or Scenario document to an OpenTimelineIO JSON string."""
    if not isinstance(document, dict):
        raise ValueError("Storyboard document must be a JSON object")
    try:
        rate = float(fps)
    except (TypeError, ValueError) as error:
        raise ValueError("fps must be a positive number") from error
    if not (1.0 <= rate <= 120.0):
        raise ValueError("fps must be between 1 and 120")

    scenes = _extract_scenes(document)
    title = str(
        document.get("title")
        or document.get("topic")
        or document.get("job_id")
        or document.get("scenario_id")
        or "Vertep Storyboard"
    ).strip()
    meta = {
        "source_kind": source_kind,
        "job_id": document.get("job_id"),
        "scenario_id": document.get("scenario_id") or document.get("id"),
        "version": document.get("active_version", document.get("version")),
        "brand_id": document.get("brand_id") or document.get("owner_brand_id"),
        "fps": rate,
        "scene_count": len(scenes),
    }

    try:
        return _build_with_otio_library(title, scenes, rate, meta)
    except ImportError:
        return _build_pure_json_otio(title, scenes, rate, meta)
