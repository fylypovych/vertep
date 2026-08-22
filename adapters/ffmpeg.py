import os
import shutil
import subprocess
from pathlib import Path


class FFmpegAdapter:
    PRESETS = {"youtube": (1280, 720, 25), "tiktok": (720, 1280, 30),
               "reels": (720, 1280, 30), "square": (1080, 1080, 25)}
    def _executable(self) -> str:
        executable = shutil.which("ffmpeg")
        if executable:
            return executable
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError, OSError) as error:
            raise RuntimeError("ffmpeg executable is not installed") from error

    def concat_audio(self, sources: list[Path], output: Path) -> Path:
        if not sources:
            raise ValueError("At least one audio source is required")
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [self._executable(), "-y"]
        for source in sources:
            command.extend(["-i", str(source)])
        inputs = "".join(f"[{index}:a]" for index in range(len(sources)))
        command.extend(["-filter_complex", f"{inputs}concat=n={len(sources)}:v=0:a=1[out]",
                        "-map", "[out]", "-c:a", "pcm_s16le", str(output)])
        subprocess.run(command, check=True, capture_output=True)
        return output

    def assemble_clips(self, output: Path, clips: list[Path], audio: Path | None = None,
                       subtitles: Path | None = None, aspect_ratio: str = "16:9",
                       preset: str | None = None) -> Path:
        if not clips:
            raise ValueError("At least one video clip is required")
        output.parent.mkdir(parents=True, exist_ok=True)
        width, height, fps = self.PRESETS.get(preset or "", (720, 1280, 30) if aspect_ratio == "9:16" else (1280, 720, 25))
        command = [self._executable(), "-y"]
        for clip in clips:
            command.extend(["-i", str(clip)])
        audio_index = None
        if audio and audio.is_file():
            audio_index = len(clips)
            command.extend(["-i", str(audio)])
        filters = []
        labels = []
        for index in range(len(clips)):
            label = f"clip{index}"
            filters.append(f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                           f"format=yuv420p,setpts=PTS-STARTPTS[{label}]")
            labels.append(f"[{label}]")
        filters.append(f"{''.join(labels)}concat=n={len(clips)}:v=1:a=0[combined]")
        video_label = "combined"
        if subtitles and subtitles.is_file() and os.getenv("BURN_SUBTITLES", "false").lower() == "true":
            escaped = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"[combined]subtitles='{escaped}'[subtitled]")
            video_label = "subtitled"
        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(fps)])
        if audio_index is not None:
            command.extend(["-map", f"{audio_index}:a", "-c:a", "aac", "-shortest"])
        command.extend(["-movflags", "+faststart", str(output)])
        subprocess.run(command, check=True, capture_output=True)
        self.probe(output)
        return output

    def assemble(self, output: Path, image: Path | None = None, images: list[Path] | None = None,
                 durations: list[float] | None = None, audio: Path | None = None,
                 music: Path | None = None, subtitles: Path | None = None, aspect_ratio: str = "16:9",
                 preset: str | None = None, watermark: Path | None = None) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        sources = images or ([image] if image else [])
        if not sources:
            raise ValueError("At least one image is required")
        width, height, fps = self.PRESETS.get(preset or "", (720, 1280, 30) if aspect_ratio == "9:16" else (1280, 720, 25))
        scene_durations = durations or [5.0] * len(sources)
        if len(scene_durations) < len(sources):
            scene_durations.extend([5.0] * (len(sources) - len(scene_durations)))

        command = [self._executable(), "-y"]
        for source, duration in zip(sources, scene_durations):
            command.extend(["-loop", "1", "-t", str(max(0.2, duration)), "-i", str(source)])
        watermark_index = None
        if watermark and watermark.is_file():
            watermark_index = len(sources)
            command.extend(["-i", str(watermark)])
        audio_index = None
        if audio and audio.is_file():
            audio_index = len(sources) + (1 if watermark_index is not None else 0)
            command.extend(["-i", str(audio)])
        music_index = None
        if music and music.is_file():
            music_index = len(sources) + (1 if watermark_index is not None else 0) + (1 if audio_index is not None else 0)
            command.extend(["-stream_loop", "-1", "-i", str(music)])

        filters = []
        video_labels = []
        for index, duration in enumerate(scene_durations[:len(sources)]):
            frames = max(5, int(duration * 25))
            label = f"v{index}"
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"zoompan=z='min(zoom+0.0008,1.08)':d={frames}:s={width}x{height}:fps={fps},"
                f"fade=t=in:st=0:d=0.25,fade=t=out:st={max(0, duration - 0.25)}:d=0.25[{label}]"
            )
            video_labels.append(f"[{label}]")
        filters.append(f"{''.join(video_labels)}concat=n={len(sources)}:v=1:a=0[combined]")
        output_label = "combined"
        if watermark_index is not None:
            filters.append(f"[{watermark_index}:v]scale={max(80, width // 7)}:-1[logo]")
            filters.append("[combined][logo]overlay=W-w-24:H-h-24[branded]")
            output_label = "branded"
        if subtitles and subtitles.is_file() and os.getenv("BURN_SUBTITLES", "false").lower() == "true":
            escaped = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"[{output_label}]subtitles='{escaped}'[subtitled]")
            output_label = "subtitled"
        audio_label = None
        if audio_index is not None and music_index is not None:
            filters.append(f"[{audio_index}:a]loudnorm=I=-16:LRA=11:TP=-1.5[voice]")
            filters.append(f"[{music_index}:a]volume=0.15[music]")
            filters.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=2[mixed]")
            audio_label = "mixed"
        elif audio_index is not None:
            filters.append(f"[{audio_index}:a]loudnorm=I=-16:LRA=11:TP=-1.5[mixed]")
            audio_label = "mixed"
        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{output_label}]"])
        if audio_label:
            command.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-shortest"])
        command.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(fps),
                        "-movflags", "+faststart", str(output)])
        subprocess.run(command, check=True, capture_output=True)
        if not output.exists() or output.stat().st_size < 100:
            raise RuntimeError("ffmpeg did not create a valid output")
        self.probe(output)
        return output

    def probe(self, path: Path) -> dict:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            candidate = Path(self._executable()).with_name("ffprobe" + (".exe" if os.name == "nt" else ""))
            ffprobe = str(candidate) if candidate.exists() else None
        if not ffprobe:
            data = path.read_bytes()[:32]
            if b"ftyp" not in data:
                raise RuntimeError("Output is not a recognized MP4 container")
            return {"format": "mp4", "validated": "container"}
        import json
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration,format_name",
                                 "-of", "json", str(path)], check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        if float(payload.get("format", {}).get("duration", 0)) <= 0:
            raise RuntimeError("ffprobe reported an invalid duration")
        return payload
