"""Deterministic browser fixture: 120 seconds of color and tone, NOT real speech."""
from pathlib import Path
import subprocess
import sys
import shutil


if __name__ == "__main__":
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=0x183b35:s=640x360:r=10:d=120", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=120", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(output),
    ], check=True)
    if len(sys.argv) > 2:
        media = Path(sys.argv[2])
        media.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output, media / "sample.mp4")
        for extension, codecs in (("mkv", ["-c", "copy"]),
                                  ("webm", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus"])):
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(output),
                            *codecs, str(media / f"sample.{extension}")], check=True)
        for codec, encoder in (("av1", "libaom-av1"), ("vp9", "libvpx-vp9")):
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "color=red:s=160x96:r=5:d=1", "-f", "lavfi", "-i", "sine=duration=1",
                            "-c:v", encoder, "-cpu-used", "8", "-threads", "1", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(media / f"sample-{codec}.mp4")], check=True)
    print("Created browser fixture (color + tone, fake recognition only).")
