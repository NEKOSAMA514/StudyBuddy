"""Exercise the standalone build with reference-generated media; no PyAV or models."""
import argparse
import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import wave


PREVIEW_FILTER = "scale=w='trunc(min(1280,min(iw,min(720,ih)*dar))/2)*2':h='trunc(min(720,min(ih,min(1280,iw)/dar))/2)*2',setsar=1"


def run(args, env=None):
    result = subprocess.run([str(x) for x in args], env=env, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(subprocess.list2cmdline([str(x) for x in args]) + "\n" + result.stderr.decode("utf-8", errors="replace")[-3000:])
    return result.stdout


def verify(args):
    package, output = args.package.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixtures = output / "fixtures"
    fixtures.mkdir(exist_ok=True)
    bin_dir = package / "bin"
    ffmpeg, ffprobe = bin_dir / "ffmpeg.exe", bin_dir / "ffprobe.exe"
    env = {**os.environ, "PATH": str(bin_dir) + ";" + os.environ["SystemRoot"] + "/System32;" + os.environ["SystemRoot"]}
    report = {"status": "failed", "pyav_tested": False, "clean_machine_tested": False,
              "reference_ffmpeg": str(args.reference.resolve()), "checks": [], "media": []}

    def command(*values):
        return run([ffmpeg, "-nostdin", "-v", "error", "-y", *values], env)

    def probe(path):
        return json.loads(run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path], env))

    def video(info):
        return next(s for s in info["streams"] if s["codec_type"] == "video")

    def preview(source, target):
        command("-copyts", "-start_at_zero", "-i", source, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                "-vf", PREVIEW_FILTER, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-fps_mode", "vfr", "-movflags", "+faststart", target)
        info = probe(target)
        track = video(info)
        assert track["codec_name"] == "h264" and track["pix_fmt"] == "yuv420p"
        assert track["width"] <= 1280 and track["height"] <= 720
        assert all(s["codec_name"] == "aac" for s in info["streams"] if s["codec_type"] == "audio")
        return info

    def audio(source, target):
        command("-copyts", "-start_at_zero", "-i", source, "-map", "0:a:0", "-vn", "-af", "aresample=async=1:first_pts=0",
                "-ar", "16000", "-ac", "1", target)
        with wave.open(str(target)) as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
            assert stream.getnframes() > 0
            return array.array("h", stream.readframes(stream.getnframes()))

    def reference(*values):
        return run([args.reference, "-nostdin", "-v", "error", "-y", *values])

    try:
        version = run([ffmpeg, "-version"], env).decode()
        configuration = run([ffmpeg, "-hide_banner", "-buildconf"], env).decode()
        external = set(re.findall(r"--enable-(lib[\w-]+)", configuration))
        assert external == {"libx264", "libdav1d"}, external
        for option in ("--enable-shared", "--enable-gpl", "--enable-version3", "--disable-autodetect", "--enable-schannel"):
            assert option in configuration, option
        report["version"] = version.splitlines()[0]
        report["checks"].append("configuration: only x264 and dav1d external media libraries")

        local_dlls = {p.name.lower() for p in bin_dir.glob("*.dll")}
        dependencies = {}
        for path in [ffmpeg, ffprobe, *sorted(bin_dir.glob("*.dll"))]:
            with path.open("rb") as stream:
                stream.seek(0x3C)
                offset = struct.unpack("<I", stream.read(4))[0]
                stream.seek(offset)
                assert stream.read(4) == b"PE\0\0" and struct.unpack("<H", stream.read(2))[0] == 0x8664, path.name
            data = run([args.dumpbin, "/NOLOGO", "/DEPENDENTS", path]).decode("utf-8", errors="replace")
            imported = re.findall(r"(?mi)^\s+([\w.-]+\.dll)\s*$", data)
            dependencies[path.name] = imported
            for name in imported:
                lower = name.lower()
                assert not lower.startswith(("msys-", "libgcc", "libwinpthread", "vcruntime", "msvcp")), (path.name, name)
                assert lower in local_dlls or lower.startswith(("api-ms-win-", "ext-ms-win-")) or (Path(os.environ["SystemRoot"]) / "System32" / name).is_file(), (path.name, name)
        report["dependencies"] = dependencies
        for name in ("avcodec", "avdevice", "avfilter", "avformat", "avutil", "swresample", "swscale"):
            assert list((package / "include" / ("lib" + name)).glob("*.h")), name
            assert (package / "lib" / (name + ".lib")).is_file(), name
            assert any(p.startswith(name + "-") for p in local_dlls), name
        report["checks"].append("x64 PE, isolated PATH, Windows/packaged DLL dependencies and development files")

        cases = [
            ("h264.mp4", "libx264", "aac"), ("hevc.mkv", "libx265", "aac"),
            ("vp9.webm", "libvpx-vp9", "libopus"), ("av1.mp4", "libaom-av1", "aac"),
            ("hevc_10bit.mkv", "libx265", "aac"), ("av1_10bit.mp4", "libaom-av1", "aac"),
            ("mpeg4.mov", "mpeg4", "pcm_s16le"), ("mpeg4.avi", "mpeg4", "libmp3lame"),
            ("flv.flv", "flv", "libmp3lame"), ("wmv.wmv", "wmv2", "wmav2"),
            ("mpeg.mpg", "mpeg2video", "mp2"), ("mpeg.ts", "mpeg2video", "mp2"),
            ("theora.ogv", "libtheora", "libvorbis")]
        for name, encoder, audio_encoder in cases:
            options = ["-cpu-used", "8"] if encoder in {"libvpx-vp9", "libaom-av1"} else []
            if encoder == "libx265":
                options = ["-x265-params", "pools=1:frame-threads=1"]
            reference("-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=0.8", "-f", "lavfi", "-i", "sine=duration=0.8:sample_rate=44100",
                      "-c:v", encoder, *options, "-c:a", audio_encoder, "-pix_fmt", "yuv420p10le" if "10bit" in name else "yuv420p", "-threads", "1", fixtures / name)
        # Generate all input media with the old build before processing it with the candidate.
        reference("-f", "lavfi", "-i", "color=red:s=1920x1080:r=10:d=0.8", "-c:v", "mpeg4", fixtures / "silent.avi")
        reference("-f", "lavfi", "-i", "color=red:s=720x576:r=10:d=0.8", "-vf", "setsar=16/15", "-c:v", "mpeg4", fixtures / "aspect.mov")
        reference("-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=1", "-f", "lavfi", "-i", "color=blue:s=160x120:r=10:d=1",
                  "-f", "lavfi", "-i", "sine=duration=2:sample_rate=44100", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                  "-map", "[v]", "-map", "2:a", "-fps_mode", "vfr", "-c:v", "libx264", "-c:a", "aac", "-output_ts_offset", "5", fixtures / "vfr.mkv")
        reference("-f", "lavfi", "-i", "color=red:s=160x120:r=10:d=2", "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=duration=1.5:sample_rate=44100",
                  "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", "-output_ts_offset", "5", fixtures / "delayed.mkv")
        for name, _, _ in cases:
            source = fixtures / name
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            info = probe(source)
            command("-xerror", "-i", source, "-t", "1", "-f", "null", "-")
            decoded_audio = audio(source, output / (name + ".wav"))
            assert max(abs(x) for x in decoded_audio) > 500
            image = output / (name + ".jpg")
            command("-ss", "0.25", "-i", source, "-map", "0:v:0", "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2", image)
            assert video(probe(image))["codec_name"] == "mjpeg"
            command("-xerror", "-i", image, "-f", "null", "-")
            pixel = command("-i", image, "-vf", "scale=1:1", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
            assert len(pixel) == 3 and pixel[0] > 150 and pixel[1] < 80 and pixel[2] < 80, (name, list(pixel))
            playback = preview(source, output / (name + ".preview.mp4"))
            assert abs(float(playback["format"]["duration"]) - float(info["format"]["duration"])) < 0.25
            assert before == hashlib.sha256(source.read_bytes()).hexdigest()
            report["media"].append({"file": name, "video_codec": video(info)["codec_name"], "checks": ["probe", "decode", "wav-16k-mono", "jpeg", "h264-aac-preview", "source-unchanged"]})
            print("MEDIA OK " + name, flush=True)
        report["checks"].append("13 real input samples; 10 container types; H.264/HEVC/VP9/AV1, 10-bit HEVC/AV1 and common audio")

        silent = preview(fixtures / "silent.avi", output / "silent.mp4")
        assert not any(s["codec_type"] == "audio" for s in silent["streams"])
        assert (video(silent)["width"], video(silent)["height"]) == (1280, 720)
        aspect = video(preview(fixtures / "aspect.mov", output / "aspect.mp4"))
        assert math.isclose(aspect["width"] / aspect["height"], 4 / 3, abs_tol=0.01)
        original = probe(fixtures / "vfr.mkv")
        assert float(original["format"]["start_time"]) > 4
        vfr = preview(fixtures / "vfr.mkv", output / "vfr.mp4")
        assert abs(float(vfr["format"]["start_time"])) < 0.1
        frames = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "json", output / "vfr.mp4"], env))["frames"]
        times = [float(f["pts_time"]) for f in frames]
        assert all(b > a for a, b in zip(times, times[1:]))
        assert len({round(b - a, 3) for a, b in zip(times, times[1:])}) > 1
        samples = audio(fixtures / "delayed.mkv", output / "delayed.wav")
        assert max(abs(x) for x in samples[:4000]) < 100
        onset = next(i for i, value in enumerate(samples) if abs(value) > 500) / 16000
        assert abs(onset - 0.5) < 0.06, onset
        report["checks"].append("silent input, 720p bound, aspect ratio, VFR, nonzero origin and delayed-audio silence")

        command("-i", fixtures / "h264.mp4", "-c", "copy", output / "remux.mkv")
        command("-i", output / "remux.mkv", "-c", "copy", "-movflags", "+faststart", output / "remux.mp4")
        command("-i", fixtures / "h264.mp4", "-an", "-c:v", "copy", output / "video.mp4")
        command("-i", fixtures / "h264.mp4", "-vn", "-c:a", "copy", output / "audio.m4a")
        command("-i", output / "video.mp4", "-i", output / "audio.m4a", "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", output / "merged.mp4")
        assert {s["codec_type"] for s in probe(output / "merged.mp4")["streams"]} == {"audio", "video"}
        assert video(probe(output / "remux.mp4"))["codec_name"] == "h264"
        report["checks"].append("lossless remux and separate audio/video merge")
        report["status"] = "passed"
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dumpbin", type=Path, required=True)
    verify(parser.parse_args())
