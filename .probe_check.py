import math
import struct
import subprocess
import tempfile
import wave
import zlib
from pathlib import Path

import numpy as np
from adapters.ffmpeg import FFmpegAdapter

binary = FFmpegAdapter()._executable()
root = Path(tempfile.mkdtemp())


def png_bytes(colour, width=486, height=864):
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def tone(path, seconds, frequency, rate=24000, amplitude=12000):
    samples = bytearray()
    for index in range(int(rate * seconds)):
        value = int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
        samples += value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(samples))
    return path


adapter = FFmpegAdapter()
image = root / "scene.png"
image.write_bytes(png_bytes((200, 10, 10)))
clip = root / "scene.mp4"
adapter.pre_cut(image, 4.0, clip)

voice = tone(root / "voice.wav", 4.0, 440)
music = tone(root / "music.wav", 4.0, 220)
logo = root / "logo.png"
logo.write_bytes(png_bytes((255, 255, 255), 120, 120))
srt = root / "voice.srt"
srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nПРИВІТ СВІТ\n\n", encoding="utf-8")

for label, subtitles, music_path, watermark in (
    ("plain", None, None, None),
    ("srt", srt, None, None),
    ("bgm", None, music, None),
    ("logo", None, None, logo),
):
    out = root / f"{label}.mp4"
    adapter.apply_post_step(out, clip, audio=voice, music=music_path, subtitles=subtitles,
                            aspect_ratio="9:16", watermark=watermark)
    print(label, "ok", out.stat().st_size)


def frame(path, at, width, height):
    done = subprocess.run([binary, "-nostdin", "-loglevel", "error", "-ss", f"{at}", "-i",
                           str(path), "-frames:v", "1",
                           "-vf", f"scale={width}:{height},format=rgb24", "-f", "rawvideo",
                           "-"], capture_output=True)
    return np.frombuffer(done.stdout, dtype=np.uint8).reshape(height, width, 3)


def audio(path, rate=16000):
    done = subprocess.run([binary, "-nostdin", "-loglevel", "error", "-i", str(path), "-vn",
                           "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                          capture_output=True)
    return np.frombuffer(done.stdout, dtype="<i2").astype(float)


plain = frame(root / "plain.mp4", 2.0, 720, 1280)
srt_frame = frame(root / "srt.mp4", 2.0, 720, 1280)
print("srt bottom band delta:", float(np.abs(srt_frame[-250:].mean(axis=2) - plain[-250:].mean(axis=2)).mean()))
print("srt top band delta:", float(np.abs(srt_frame[:250].mean(axis=2) - plain[:250].mean(axis=2)).mean()))

logo_frame = frame(root / "logo.mp4", 2.0, 720, 1280)
print("logo corner delta:", float(np.abs(logo_frame[-200:, -200:] - plain[-200:, -200:]).mean()))

for label in ("plain", "bgm"):
    window = audio(root / f"{label}.mp4")
    spectrum = np.abs(np.fft.rfft(window))
    freqs = np.fft.rfftfreq(window.size, d=1.0 / 16000)
    peaks = sorted(np.argsort(spectrum)[-6:])
    print(label, "dominant", round(float(freqs[int(np.argmax(spectrum))]), 1),
          "rms", round(float(np.sqrt(np.mean(window ** 2)) / 32768), 4),
          "top freqs", sorted(round(float(freqs[p]), 1) for p in peaks))

def _png_bytes(colour):
    import struct
    import zlib

    rgb = {"red": (200, 10, 10), "green": (10, 200, 10), "blue": (10, 10, 200)}[colour]
    width = height = 486
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


root = Path(tempfile.mkdtemp())
adapter = FFmpegAdapter()
binary = adapter._executable()
print("binary:", binary)


scenes = [("red", 2), ("green", 3), ("blue", 4)]
clips = []
for colour, seconds in scenes:
    path = root / f"{colour}.png"
    path.write_bytes(_png_bytes(colour))
    target = root / f"{colour}.mp4"
    adapter.pre_cut(path, seconds, target)
    clips.append(target)

voice = root / "voice.wav"
import math
import wave

rate = 16000
total = sum(s for _, s in scenes)
tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * total)))
with wave.open(str(voice), "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    handle.writeframes(tone)

out = root / "combined.mp4"
adapter.assemble_clips(out, clips, audio=voice, aspect_ratio="9:16")

completed = subprocess.run([binary, "-nostdin", "-hide_banner", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
stderr = completed.stderr
print([line.strip() for line in stderr.splitlines() if "Stream #0:0" in line or "Duration" in line])

match = re.search(r"Video:.*?, (\d+)x(\d+)", stderr)
size = (int(match.group(1)), int(match.group(2))) if match else None
print("size:", size)

frame = subprocess.run([binary, "-nostdin", "-loglevel", "error", "-ss", "4.5", "-i", str(out),
                        "-frames:v", "1", "-vf", f"scale={size[0]}:{size[1]},format=rgb24",
                        "-f", "rawvideo", "-"], capture_output=True)
array = np.frombuffer(frame.stdout, dtype=np.uint8)
print("bytes:", array.size, "expected:", size[0] * size[1] * 3)
print("mean colour at 4.5s:", array.reshape(-1, 3).mean(axis=0))

audio = subprocess.run([binary, "-nostdin", "-loglevel", "error", "-i", str(out), "-vn",
                        "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True)
window = np.frombuffer(audio.stdout, dtype="<i2").astype(float)
spectrum = np.abs(np.fft.rfft(window))
freqs = np.fft.rfftfreq(window.size, d=1.0 / 16000)
print("dominant:", freqs[int(np.argmax(spectrum))], "rms:", float(np.sqrt(np.mean(window ** 2)) / 32768))
