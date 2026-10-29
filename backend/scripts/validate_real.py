"""生成两分钟自编教学样本；可选真实 CPU ASR/OCR 验证，无云端模型调用。

python scripts/validate_real.py --media-only
python scripts/validate_real.py
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.recognition.media import Recognizer
from app.videos.video_reader import extract_audio, extract_frame, probe


def run(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def sample(folder):
    path = folder / "sample.mp4"
    if path.exists():
        return path
    headings = ["TCP CONGESTION CONTROL", "SLOW START", "CONGESTION AVOIDANCE"]
    narration = [
        "Welcome to this short lesson about computer networks. Today we explain TCP congestion control. The sender adjusts how much data is in flight, so that a busy network is not overloaded. We will discuss slow start and congestion avoidance.",
        "Slow start begins with a small congestion window. As acknowledgements arrive, the sender increases the window. The window grows quickly. This phase continues until a threshold is reached or packet loss is detected. Slow start does not mean the window always grows slowly.",
        "In congestion avoidance, the congestion window grows more cautiously. The goal is to use available capacity without overwhelming the network. Packet loss can signal congestion. The sender then reduces its sending window. Always compare the explanation with the original lesson.",
    ]
    for i, (heading, text) in enumerate(zip(headings, narration)):
        wav = folder / f"voice-{i}.wav"
        run(["espeak-ng", "-v", "en-us", "-s", "135", "-w", str(wav), text])
        draw = f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{heading}':fontcolor=white:fontsize=42:x=(w-tw)/2:y=h/2-30,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='StudyBuddy - synthetic validation clip':fontcolor=white:fontsize=22:x=(w-tw)/2:y=h/2+45"
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x183b35:s=1280x720:r=24:d=40", "-i", str(wav), "-vf", draw, "-af", "apad", "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(folder / f"part-{i}.mp4")])
    listing = folder / "concat.txt"
    listing.write_text("\n".join(f"file '{folder / f'part-{i}.mp4'}'" for i in range(3)), encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(path)])
    return path


def chinese_sample(folder):
    source = folder / "chinese"
    path = folder / "sample-chinese.mp4"
    if path.exists():
        return path
    for i in range(3):
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(source / f"slide-{i}.png"), "-i", str(source / f"voice-{i}.wav"), "-af", "apad", "-t", "40", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source / f"part-{i}.mp4")])
    listing = source / "concat.txt"
    listing.write_text("\n".join(f"file '{source / f'part-{i}.mp4'}'" for i in range(3)), encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(path)])
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-only", action="store_true")
    parser.add_argument("--chinese", action="store_true")
    args = parser.parse_args()
    settings = Settings() if args.chinese else Settings(asr_engine="whisper", asr_model="base", asr_language="en")
    folder = settings.data_dir / "validation"
    folder.mkdir(parents=True, exist_ok=True)
    path = chinese_sample(folder) if args.chinese else sample(folder)
    info = probe(path, 1200)
    print(json.dumps({"sample": str(path), "info": info}), flush=True)
    if args.media_only:
        return
    recognizer = Recognizer(settings)
    started = time.monotonic()
    audio = folder / "sample.wav"
    extract_audio(path, audio)
    speech = recognizer.transcribe(audio)
    asr_seconds = time.monotonic() - started
    assert speech, "真实语音识别没有结果"
    assert all(0 <= e["start_seconds"] <= e["end_seconds"] <= info["duration"] for e in speech)
    ocr, started = [], time.monotonic()
    for seconds in [0, 15, 45, 75, 105]:
        image = folder / f"check-{seconds}.jpg"
        extract_frame(path, image, seconds)
        text, confidence = recognizer.ocr(image)
        ocr.append({"time": seconds, "text": text, "confidence": confidence})
    assert all(e["text"] for e in ocr), "抽样画面 OCR 无结果"
    report = {"sample_type": "两分钟中文合成讲解 + 清晰中文幻灯片，非真实普通话课程" if args.chinese else "两分钟英文合成讲解 + 清晰英文幻灯片，非真实普通话课程",
              "duration": info["duration"], "asr_seconds": round(asr_seconds, 2),
              "ocr_seconds": round(time.monotonic() - started, 2), "asr_model": settings.asr_model,
              "speech": speech, "ocr": ocr, "cloud_model_tested": False}
    if args.chinese:
        report["anchor_checks"] = []
        for phrase, expected in [("欢迎学习", 0), ("现在介绍慢启动", 40), ("最后介绍", 80)]:
            match = next((s for s in speech if phrase in s["text"]), None)
            report["anchor_checks"].append({"phrase": phrase, "expected_start": expected,
                                           "actual_start": match["start_seconds"] if match else None})
    (folder / ("chinese-recognition.json" if args.chinese else "real-recognition.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"asr_seconds": report["asr_seconds"], "ocr_seconds": report["ocr_seconds"], "speech_segments": len(speech), "ocr_frames": len(ocr), "cloud_model_tested": False}), flush=True)


if __name__ == "__main__":
    main()
