"""Text model and voice model management routes for the Vertep CORE web application."""
import io

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from adapters.providers import provider_matrix
from worker.role_executor import (delete_text_model, list_text_models, list_voices,
                                  synthesize_voice)

router = APIRouter()

_PREVIEW_TEXT_LIMIT = 500
_TTS_DISABLED_BACKENDS = {"none", "", "placeholder"}


@router.get("/api/models/text")
def list_text_models_endpoint():
    try:
        return {"models": list_text_models()}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Ollama is unreachable: {error}") from error


@router.post("/api/models/text/pull")
def pull_text_model_endpoint(payload: dict):
    """Start an async local pull and return the durable operation record."""
    model = payload.get("model")
    if not model or not isinstance(model, str):
        raise HTTPException(422, "model is required")
    from core.pull_executor import PlacementError, start_pull
    try:
        return start_pull(model, requested_by="web",
                          node_name=str(payload.get("node") or "") or None)
    except PlacementError as error:
        raise HTTPException(error.status_code, error.message) from error


@router.delete("/api/models/text/{model:path}")
def delete_text_model_endpoint(model: str):
    try:
        delete_text_model(model)
        return {"deleted": model}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Ollama delete failed: {error}") from error


@router.get("/api/models/voices")
def list_voices_endpoint():
    try:
        return {"voices": list_voices()}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"TTS runtime is unreachable: {error}") from error


@router.post("/api/models/voices/synthesize")
def synthesize_voice_endpoint(payload: dict):
    text = payload.get("text")
    if not text or not isinstance(text, str):
        raise HTTPException(422, "text is required")
    voice = payload.get("voice", "default")
    speed = int(payload.get("speed", 150))
    try:
        audio = synthesize_voice(text, voice, speed)
        return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")
    except httpx.HTTPError as error:
        raise HTTPException(502, f"TTS synthesis failed: {error}") from error


def _tts_backend() -> str:
    return str(provider_matrix().get("tts", {}).get("backend") or "").strip().lower()


@router.post("/api/models/voices/preview")
def voice_preview_endpoint(payload: dict):
    """Synthesize a short sample through the deployed TTS configuration.

    Negative paths are explicit: disabled/incomplete backend (409), invalid
    request (422), unreachable runtime or unreadable catalog (502).
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "text is required")
    if len(text) > _PREVIEW_TEXT_LIMIT:
        raise HTTPException(422, f"Preview text must be at most {_PREVIEW_TEXT_LIMIT} characters")
    try:
        speed = int(payload.get("speed", 150))
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "speed must be an integer") from error
    if not 1 <= speed <= 500:
        raise HTTPException(422, "speed must be between 1 and 500")

    tts = provider_matrix().get("tts", {})
    if str(tts.get("backend") or "").strip().lower() in _TTS_DISABLED_BACKENDS:
        raise HTTPException(409, "TTS backend is disabled — enable a provider first")
    if tts.get("configured") is False:
        raise HTTPException(409, "TTS backend is not fully configured")

    voice = str(payload.get("voice") or "default").strip() or "default"
    try:
        catalog = list_voices()
    except httpx.HTTPError as error:
        raise HTTPException(502, f"TTS runtime is unreachable: {error}") from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(502, f"TTS runtime returned an unreadable voice catalog: {error}") from error
    names = {str(item.get("name") if isinstance(item, dict) else item) for item in catalog or []}
    if names and voice not in names:
        raise HTTPException(422, f"Unknown voice: {voice}")

    try:
        audio = synthesize_voice(text, voice, speed)
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Voice preview failed: {error}") from error
    if not audio:
        raise HTTPException(502, "TTS runtime returned no audio")
    return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")
