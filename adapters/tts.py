import json
import os
import subprocess
from pathlib import Path

from .ffmpeg import FFmpegAdapter


class TTSAdapter:
    def __init__(self, provider: str | None = None):
        self.provider = provider or os.getenv("TTS_PROVIDER", "none")

    def configured(self) -> bool:
        return self.provider in {"mock", "none"} or bool(os.getenv(f"{self.provider.upper()}_API_KEY", ""))

    def synthesize(self, text: str, output: Path, duration: float = 3.0, voice: dict | None = None) -> dict:
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.provider == "none":
            return {"status": "SKIPPED", "provider": "none"}
        if self.provider != "mock":
            return {"status": "NOT_CONFIGURED" if not self.configured() else "FAILED",
                    "provider": self.provider, "error": "Live TTS provider adapter is not implemented"}
        command = [FFmpegAdapter()._executable(), "-y", "-f", "lavfi", "-i",
                   f"sine=frequency=220:sample_rate=48000:duration={max(0.2, duration)}",
                   "-af", "volume=0.03", "-c:a", "pcm_s16le", str(output)]
        subprocess.run(command, check=True, capture_output=True)
        manifest = {"status": "READY", "provider": "mock", "text": text, "duration": duration,
                    "voice": voice or {}, "path": str(output)}
        output.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
