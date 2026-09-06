"""Text model and voice model management routes for the Vertep CORE web application."""
import io

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from worker.role_executor import (delete_text_model, list_text_models, list_voices,
                                  pull_text_model, synthesize_voice)

router = APIRouter()


@router.get("/api/models/text")
def list_text_models_endpoint():
    try:
        return {"models": list_text_models()}
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Ollama is unreachable: {error}") from error


@router.post("/api/models/text/pull")
def pull_text_model_endpoint(payload: dict):
    model = payload.get("model")
    if not model or not isinstance(model, str):
        raise HTTPException(422, "model is required")
    try:
        return pull_text_model(model)
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Ollama pull failed: {error}") from error


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