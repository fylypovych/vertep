"""Локальний TTS runtime на базі espeak-ng."""

import base64
import os
import re
import shutil
import subprocess

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep TTS", version="1")
VOICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    voice: str = Field(default="uk", max_length=32)
    speed: int = Field(default=150, ge=80, le=450)


@app.get("/health")
def health() -> dict:
    executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    if not executable:
        raise HTTPException(503, "espeak-ng недоступний")
    return {"status": "HEALTHY", "engine": "espeak-ng"}


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict:
    if not VOICE_RE.fullmatch(request.voice):
        raise HTTPException(422, "Некоректний ідентифікатор голосу")
    executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    if not executable:
        raise HTTPException(503, "espeak-ng недоступний")
    try:
        result = subprocess.run(
            [executable, "--stdout", "-v", request.voice, "-s", str(request.speed), request.text],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise HTTPException(502, "TTS engine не зміг синтезувати аудіо") from error
    if not result.stdout.startswith(b"RIFF"):
        raise HTTPException(502, "TTS engine повернув некоректний WAV")
    return {"audio_base64": base64.b64encode(result.stdout).decode("ascii"),
            "mime_type": "audio/wav", "engine": "espeak-ng", "voice": request.voice}
