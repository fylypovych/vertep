"""Локальний гібридний TTS runtime: Piper (основний для uk_UA) + espeak-ng (запасний)."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep TTS", version="2")
VOICE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# The Voice Worker self-test and preview send "default": the engine's own default
# voice (Piper uk_UA when installed, otherwise espeak-ng without -v) (#455, #540).
DEFAULT_VOICE = "default"

# espeak-ng ships its voice table under this directory; the list is read once
# at startup and cached so /voices and /synthesize agree on what is supported.
_VOICE_DATA_DIR = os.environ.get("ESPEAK_VOICE_DIR", "/usr/lib/espeak-ng-data/voices")

# Piper ONNX models (.onnx + .onnx.json) bundled in the tts container (#540).
_PIPER_MODELS_DIR = os.environ.get("PIPER_MODELS_DIR", "/opt/piper-models")
_PIPER_DEFAULT_UK_VOICE = os.environ.get("PIPER_DEFAULT_UK_VOICE", "uk_UA-ukrainian_tts-medium")

# Readable espeak-ng VoiceName per voice id, e.g. "uk" -> "Ukrainian".
VOICE_NAMES: dict[str, str] = {}

PIPER_VOICE_LABELS: dict[str, str] = {
    "uk_UA-ukrainian_tts-medium": "Ukrainian (Piper medium)",
    "uk_UA-lada-x_low": "Ukrainian Lada (Piper x_low)",
}


def _normalize_language(tag: str) -> str:
    return str(tag).strip().lower().split("_")[0].split("-")[0]


def _piper_executable() -> str | None:
    return shutil.which(os.getenv("PIPER_EXECUTABLE", "piper"))


def _piper_models_dir() -> Path:
    return Path(os.environ.get("PIPER_MODELS_DIR", _PIPER_MODELS_DIR))


def _available_piper_voices() -> dict[str, Path]:
    """Enumerate Piper voices backed by both an executable and .onnx + .onnx.json files."""
    if not _piper_executable():
        return {}
    directory = _piper_models_dir()
    if not directory.is_dir():
        return {}
    found: dict[str, Path] = {}
    for entry in sorted(directory.iterdir()):
        if entry.suffix != ".onnx" or not entry.is_file():
            continue
        config_path = Path(f"{entry}.json")
        if not config_path.is_file():
            continue
        voice_id = entry.stem
        if VOICE_RE.fullmatch(voice_id):
            found[voice_id] = entry
    return found


def _piper_voice_language(voice_id: str, model_path: Path) -> str:
    config_path = Path(f"{model_path}.json")
    try:
        meta = json.loads(config_path.read_text(encoding="utf-8"))
        lang = meta.get("language")
        if isinstance(lang, dict):
            code = lang.get("family") or lang.get("code")
            if code:
                return _normalize_language(str(code))
        elif isinstance(lang, str) and lang.strip():
            return _normalize_language(lang)
    except (OSError, ValueError, TypeError):
        pass
    return _normalize_language(voice_id)


def _piper_voice_label(voice_id: str, model_path: Path) -> str:
    if voice_id in PIPER_VOICE_LABELS:
        return PIPER_VOICE_LABELS[voice_id]
    config_path = Path(f"{model_path}.json")
    try:
        meta = json.loads(config_path.read_text(encoding="utf-8"))
        dataset = str(meta.get("dataset") or "").strip()
        lang = meta.get("language")
        lang_name = lang.get("name_english") if isinstance(lang, dict) else ""
        if lang_name and dataset:
            return f"{lang_name} ({dataset})"
    except (OSError, ValueError, TypeError):
        pass
    return voice_id


def _default_uk_piper_voice(piper_voices: dict[str, Path]) -> str | None:
    preferred = os.environ.get("PIPER_DEFAULT_UK_VOICE", _PIPER_DEFAULT_UK_VOICE)
    if preferred in piper_voices and _piper_voice_language(preferred, piper_voices[preferred]) == "uk":
        return preferred
    for voice_id, path in sorted(piper_voices.items()):
        if _piper_voice_language(voice_id, path) == "uk":
            return voice_id
    return None


def _available_voices() -> list[str]:
    """Enumerate the espeak-ng voices actually installed on this host.

    Returns an empty list when the voice table cannot be read, so the runtime
    never advertises a catalog it cannot back (Issue i.0.0.1.5: a missing
    catalog must not be silently treated as "accepts anything").
    """
    directory = _VOICE_DATA_DIR
    if not os.path.isdir(directory):
        executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
        if not executable:
            return []
        try:
            result = subprocess.run([executable, "--voices"], check=True,
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return []
        # Stock espeak-ng distributes language files, not .idx/.list files.
        # Exclude MBROLA voices: that separate engine is not this runtime.
        names = {parts[1]: parts[3].replace("_", " ") for line in result.stdout.splitlines()[1:]
                 if len(parts := line.split()) >= 5
                 and not parts[4].replace("\\", "/").startswith("mb/")}
        VOICE_NAMES.update(names)
        return sorted(names)
    voices: list[str] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".idx") and not name.endswith(".list"):
            continue
        voice = name.rsplit(".", 1)[0]
        if voice:
            voices.append(voice)
    return voices


def _available_languages(installed: list[str] | None = None,
                         piper_voices: dict[str, Path] | None = None) -> list[str]:
    """Derive the language tags the hybrid TTS runtime can actually synthesize."""
    voices = installed if installed is not None else _available_voices()
    languages: set[str] = set()
    for voice in voices:
        tag = _normalize_language(voice)
        if tag:
            languages.add(tag)
    if piper_voices:
        for voice_id, path in piper_voices.items():
            tag = _piper_voice_language(voice_id, path)
            if tag:
                languages.add(tag)
    return sorted(languages)


VOICES = _available_voices()


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    voice: str = Field(default="uk", max_length=64)
    speed: int = Field(default=150, ge=80, le=450)
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)


@app.get("/health")
def health() -> dict:
    piper_voices = _available_piper_voices()
    espeak_executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    engines: list[str] = []
    if piper_voices:
        engines.append("piper")
    if espeak_executable:
        engines.append("espeak-ng")
    if not engines:
        raise HTTPException(503, "Жоден TTS engine (piper, espeak-ng) недоступний")
    return {"status": "HEALTHY", "engine": engines[0], "engines": engines}


@app.get("/voices")
def voices() -> dict:
    """Runtime voice catalog advertised to CORE in the worker heartbeat.

    Returns only voices, models, providers and languages genuinely backed by
    installed Piper ONNX models and/or the installed espeak-ng voice table.
    """
    piper_voices = _available_piper_voices()
    espeak_voices = VOICES or _available_voices()
    languages = _available_languages(espeak_voices, piper_voices)

    entries: list[dict[str, str]] = []
    for voice_id, path in sorted(piper_voices.items()):
        entries.append({
            "id": voice_id,
            "name": _piper_voice_label(voice_id, path),
            "engine": "piper",
            "provider": "piper",
            "model": voice_id,
            "language": _piper_voice_language(voice_id, path),
        })
    for voice in espeak_voices:
        entries.append({
            "id": voice,
            "name": VOICE_NAMES.get(voice, voice),
            "engine": "espeak-ng",
        })

    providers: list[str] = []
    models: list[str] = []
    engines: list[str] = []
    if piper_voices:
        providers.append("piper")
        engines.append("piper")
        models.extend(sorted(piper_voices))
        models.append("piper")
    if espeak_voices:
        providers.append("espeak-ng")
        engines.append("espeak-ng")
        models.append("espeak-ng")

    default_uk = _default_uk_piper_voice(piper_voices)
    return {
        "voices": entries,
        "default_voice": default_uk or DEFAULT_VOICE,
        "models": models,
        "languages": languages,
        "providers": providers,
        "speed_range": {"min": 80, "max": 450},
        "engine": engines[0] if engines else "espeak-ng",
        "engines": engines,
    }


def _validate_espeak_voice(voice: str, installed: list[str]) -> None:
    if not VOICE_RE.fullmatch(voice):
        raise HTTPException(422, "Некоректний ідентифікатор голосу")
    if voice == DEFAULT_VOICE:
        return
    if installed and voice not in installed:
        raise HTTPException(422, f"Голос {voice!r} не підтримується цим espeak runtime")


def _validate_espeak_language(language: str | None, installed: list[str]) -> None:
    if not language or not installed:
        return
    supported = set(_available_languages(installed))
    if _normalize_language(language) not in supported:
        raise HTTPException(422, f"Мова {language!r} не підтримується цим espeak runtime")


def normalize_wav(audio: bytes) -> bytes:
    """Return a WAV whose RIFF and ``data`` sizes match the real PCM length (#553 AC1).

    ``espeak-ng --stdout`` streams a header with a placeholder size, so decoders see
    a wrong duration. The chunks are re-walked and both sizes are rewritten from the
    bytes actually present; a payload that is not a decodable PCM WAV is refused.
    """
    import struct
    import wave
    from io import BytesIO

    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise HTTPException(502, "TTS engine повернув некоректний WAV")
    position, fmt, data_at = 12, None, None
    while position + 8 <= len(audio):
        chunk_id, size = audio[position:position + 4], struct.unpack_from("<I", audio, position + 4)[0]
        body = position + 8
        if chunk_id == b"fmt ":
            fmt = (body, min(size, len(audio) - body))
        elif chunk_id == b"data":
            data_at = body
            break
        position = body + size + (size & 1)
    if fmt is None or data_at is None or fmt[1] < 16:
        raise HTTPException(502, "TTS engine повернув некоректний WAV")
    pcm = audio[data_at:]
    header = bytearray(audio[:data_at])
    struct.pack_into("<I", header, data_at - 4, len(pcm))
    struct.pack_into("<I", header, 4, len(header) - 8 + len(pcm))
    fixed = bytes(header) + pcm
    try:
        with wave.open(BytesIO(fixed)) as handle:
            frame_bytes = handle.getnchannels() * handle.getsampwidth()
            if handle.getnframes() == 0 or frame_bytes == 0 or handle.getnframes() * frame_bytes != len(pcm) - len(pcm) % frame_bytes:
                raise HTTPException(502, "TTS engine повернув некоректний WAV")
    except (wave.Error, EOFError) as error:
        raise HTTPException(502, "TTS engine повернув некоректний WAV") from error
    return fixed


def _synthesize_with_piper(request: SynthesisRequest, voice_id: str, model_path: Path) -> dict:
    executable = _piper_executable()
    if not executable:
        raise HTTPException(503, "Piper runtime недоступний")
    length_scale = max(0.33, min(2.0, 150.0 / float(request.speed)))
    try:
        with tempfile.TemporaryDirectory(prefix="vertep-piper-") as tmp_dir:
            output_path = Path(tmp_dir) / "speech.wav"
            subprocess.run(
                [executable, "--model", str(model_path),
                 "--length-scale", f"{length_scale:.3f}",
                 "--output_file", str(output_path)],
                input=request.text.encode("utf-8"),
                check=True,
                capture_output=True,
                timeout=120,
            )
            audio = output_path.read_bytes()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise HTTPException(502, "Piper TTS engine не зміг синтезувати аудіо") from error
    audio = normalize_wav(audio)
    return {
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "mime_type": "audio/wav",
        "engine": "piper",
        "voice": voice_id,
    }


def _synthesize_with_espeak(request: SynthesisRequest, installed: list[str]) -> dict:
    _validate_espeak_voice(request.voice, installed)
    _validate_espeak_language(request.language, installed)
    if not installed:
        raise HTTPException(503, "Каталог голосів espeak-ng недоступний")
    if (request.language and request.voice != DEFAULT_VOICE
            and _normalize_language(request.voice) != _normalize_language(request.language)):
        raise HTTPException(422, "Мова не відповідає вибраному голосу")
    executable = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
    if not executable:
        raise HTTPException(503, "espeak-ng недоступний")
    try:
        result = subprocess.run(
            [executable, "--stdout", *([] if request.voice == DEFAULT_VOICE else ["-v", request.voice]),
             "-s", str(request.speed), request.text],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise HTTPException(502, "TTS engine не зміг синтезувати аудіо") from error
    audio = normalize_wav(result.stdout)
    return {
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "mime_type": "audio/wav",
        "engine": "espeak-ng",
        "voice": request.voice,
    }


def _select_piper_voice(request: SynthesisRequest, piper_voices: dict[str, Path]) -> str:
    if request.voice in piper_voices:
        target = request.voice
    elif request.model in piper_voices and request.voice in {DEFAULT_VOICE, "uk", "uk_UA"}:
        target = request.model
    elif request.voice in {DEFAULT_VOICE, "uk", "uk_UA"} and request.model in {None, "piper"}:
        default_uk = _default_uk_piper_voice(piper_voices)
        if not default_uk:
            raise HTTPException(422, f"Голос {request.voice!r} не підтримується цим Piper runtime")
        target = default_uk
    else:
        raise HTTPException(422, f"Голос {request.voice!r} не підтримується цим Piper runtime")

    if request.model not in {None, "piper", target}:
        raise HTTPException(422, "Provider/model не підтримується цим Piper runtime")
    if request.language and _normalize_language(request.language) != _piper_voice_language(target, piper_voices[target]):
        raise HTTPException(422, "Мова не відповідає вибраному голосу")
    return target


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict:
    if not VOICE_RE.fullmatch(request.voice):
        raise HTTPException(422, "Некоректний ідентифікатор голосу")

    piper_voices = _available_piper_voices()
    espeak_voices = VOICES or _available_voices()
    provider = request.provider.strip().lower() if request.provider else None
    model = request.model.strip() if request.model else None

    # 1. Explicit espeak-ng request.
    if provider == "espeak-ng" or (provider is None and model == "espeak-ng"):
        if model not in {None, "espeak-ng"}:
            raise HTTPException(422, "Provider/model не підтримується цим espeak runtime")
        return _synthesize_with_espeak(request, espeak_voices)

    # 2. Explicit Piper request (by provider, model, or Piper voice id).
    if provider == "piper" or model == "piper" or (model and model in piper_voices) or request.voice in piper_voices:
        if provider not in {None, "piper"}:
            raise HTTPException(422, "Provider/model не підтримується цим Piper runtime")
        if not piper_voices:
            raise HTTPException(422, "Piper runtime не налаштований або моделі відсутні")
        target = _select_piper_voice(request, piper_voices)
        return _synthesize_with_piper(request, target, piper_voices[target])

    # 3. Unknown explicit provider or model -> fail-closed 422.
    if provider is not None or model is not None:
        raise HTTPException(422, "Provider/model не підтримується цим TTS runtime")

    # 4. Hybrid default routing (no explicit provider/model):
    # Piper is the default for Ukrainian (uk / uk_UA / default with uk or unset language),
    # espeak-ng remains the fallback for all other languages.
    default_uk = _default_uk_piper_voice(piper_voices)
    requested_lang = _normalize_language(request.language) if request.language else None
    if default_uk and (
        (request.voice in {"uk", "uk_UA"} and requested_lang in {None, "uk"})
        or (request.voice == DEFAULT_VOICE and requested_lang in {None, "uk"})
    ):
        return _synthesize_with_piper(request, default_uk, piper_voices[default_uk])

    return _synthesize_with_espeak(request, espeak_voices)
