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

    # --- Issue #122: external-engine assembly primitives ---------------------

    PRE_CUT_FPS = 30

    def _normalise_video_filters(self, fps: int | None = None) -> str:
        rate = fps or self.PRE_CUT_FPS
        # Even dimensions keep yuv420p encoders happy; the source resolution is
        # preserved because the external engine rescales with its own fit mode.
        return (f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps={rate},"
                f"format=yuv420p,setpts=PTS-STARTPTS")

    def pre_cut(self, source: Path, duration: float, output: Path) -> Path:
        """Cut one approved asset to exactly ``duration`` seconds (§9.3).

        Still images are held for the whole scene, clips are trimmed. The result
        is silent and has a constant frame rate, which is what an external engine
        needs to concatenate it without re-timing the approved scene.
        """
        if duration <= 0:
            raise ValueError("Scene duration must be positive")
        output.parent.mkdir(parents=True, exist_ok=True)
        is_image = source.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        command = [self._executable(), "-nostdin", "-loglevel", "error", "-y"]
        if is_image:
            command.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(source)])
        else:
            command.extend(["-i", str(source)])
        command.extend([
            "-t", f"{duration:.3f}",
            "-vf", self._normalise_video_filters(),
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-r", str(self.PRE_CUT_FPS),
            "-movflags", "+faststart", str(output),
        ])
        subprocess.run(command, check=True, capture_output=True)
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(f"pre-cut produced no clip for {source.name}")
        return output

    def apply_post_step(
        self,
        output: Path,
        video: Path,
        *,
        audio: Path | None = None,
        music: Path | None = None,
        subtitles: Path | None = None,
        aspect_ratio: str = "16:9",
        preset: str | None = None,
        watermark: Path | None = None,
    ) -> Path:
        """Finish an externally rendered video exactly like a Native render (§9.5).

        The filter chain and its order mirror :meth:`assemble`: watermark overlay,
        then the conditional subtitle burn, then the audio mix. Audio timing is
        unchanged (``amix duration=first`` plus ``-shortest``), so the final file
        is audibly identical to the Native route (§9.7).
        """
        if not video.is_file():
            raise ValueError("Post-step source video is not readable")
        output.parent.mkdir(parents=True, exist_ok=True)
        width, height, fps = self.PRESETS.get(
            preset or "",
            (720, 1280, 30) if aspect_ratio == "9:16" else (1280, 720, 25),
        )
        command = [self._executable(), "-nostdin", "-loglevel", "error", "-y", "-i", str(video)]
        next_index = 1
        watermark_index = None
        if watermark and watermark.is_file():
            command.extend(["-i", str(watermark)])
            watermark_index = next_index
            next_index += 1
        audio_index = None
        if audio and audio.is_file():
            command.extend(["-i", str(audio)])
            audio_index = next_index
            next_index += 1
        music_index = None
        if music and music.is_file():
            command.extend(["-stream_loop", "-1", "-i", str(music)])
            music_index = next_index

        filters = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
            f"format=yuv420p[base]"
        ]
        video_label = "base"
        if watermark_index is not None:
            filters.append(f"[{watermark_index}:v]scale={max(80, width // 7)}:-1[logo]")
            filters.append(f"[{video_label}][logo]overlay=W-w-24:H-h-24[branded]")
            video_label = "branded"
        if subtitles and subtitles.is_file() and os.getenv("BURN_SUBTITLES", "false").lower() == "true":
            escaped = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"[{video_label}]subtitles='{escaped}'[subtitled]")
            video_label = "subtitled"

        audio_label = None
        if audio_index is not None and music_index is not None:
            filters.append(f"[{audio_index}:a]loudnorm=I=-16:LRA=11:TP=-1.5[voice]")
            filters.append(f"[{music_index}:a]volume=0.15[music]")
            filters.append("[voice][music]amix=inputs=2:duration=first:dropout_transition=2[mixed]")
            audio_label = "mixed"
        elif audio_index is not None:
            filters.append(f"[{audio_index}:a]loudnorm=I=-16:LRA=11:TP=-1.5[mixed]")
            audio_label = "mixed"

        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{video_label}]"])
        if audio_label:
            command.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-shortest"])
        command.extend([
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-r", str(fps), "-movflags", "+faststart", str(output),
        ])
        subprocess.run(command, check=True, capture_output=True)
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("post-step produced no output")
        self.probe(output)
        return output
