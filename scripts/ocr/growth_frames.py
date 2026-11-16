"""Replay the existing OCR pipeline with an optional local probe near growing text."""
import argparse
from collections import Counter
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

import cv2
import numpy as np


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
OUTPUT = PROJECT / "test-results" / "ocr-growth-frame-trial"
ARCHIVE = PROJECT.parent / "studyNote(archived)"
sys.path.insert(0, str(PROJECT / "backend"))
from app.config import Settings
from app.recognition import video_ocr
from app.recognition.extraction import VIDEO_OCR_CONFIDENCE, VIDEO_OCR_INTERVAL
from app.recognition.media import Recognizer, text_rows
from app.videos.model_input import _normalized, _requires_literal_match
from app.videos.video_reader import UniformFrameReader


CASES = {
    "gemini": (PROJECT / "data/videos/aaab0099f51d4e99b95ad6858496503d/original.mp4", 59.0),
    "ppt": (ARCHIVE / "ocr-test/original.mp4", 326.287438),
    "lecture": (ARCHIVE / "data/videos/d4f587ab08474e19ae6b81c7050cb289/original.mp4", 4615.569478),
}
STEP = .25
KERNEL = np.ones((3, 3), np.uint8)


def cut(image, box):
    left, top, right, bottom = box
    return image[top:bottom, left:right]


def gray_frame(frame):
    scale = min(1, video_ocr.DETECTOR_SIZE / max(frame.width, frame.height))
    gray = frame.reformat(width=max(32, round(frame.width * scale)),
                          height=max(32, round(frame.height * scale)), format="gray").to_ndarray()
    return np.ascontiguousarray(np.rot90(gray, (int(frame.rotation or 0) % 360) // 90))


@dataclass
class Probe:
    start: float
    anchor: tuple
    crop: tuple
    threshold: float
    light: bool
    old: np.ndarray
    base: np.ndarray
    growth_area: np.ndarray
    score: int = 0
    chosen_at: float = 0
    frame: object = None
    pixels: np.ndarray | None = None

    @classmethod
    def start_at(cls, seconds, anchor, gray):
        left, top, right, bottom = anchor
        height, width = gray.shape
        line_height = bottom - top
        if right - left < 3 * line_height:
            return None
        crop = (max(0, left - 4), max(0, top - 4),
                min(width, right + 4 * line_height), min(height, bottom + line_height + 4))
        threshold, bright = cv2.threshold(cut(gray, anchor), 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        light = np.count_nonzero(bright) < bright.size / 2
        old = bright.astype(bool) if light else ~bright.astype(bool)
        base = cut(gray, crop) > threshold
        if not light:
            base = ~base
        area = np.zeros(base.shape, dtype=bool)
        area[:, right - crop[0]:] = True
        area[bottom - crop[1]:, :] = True
        base = cv2.dilate(base.astype(np.uint8), KERNEL).astype(bool)
        return cls(seconds, anchor, crop, threshold, light, old, base, area) if old.any() else None

    def ink(self, pixels):
        return pixels > self.threshold if self.light else pixels <= self.threshold

    def retained(self, gray):
        mask = self.ink(cut(gray, self.anchor))
        near = cv2.dilate(mask.astype(np.uint8), KERNEL).astype(bool)
        return np.count_nonzero(near & self.old) / np.count_nonzero(self.old)

    def watch(self, seconds, frame, gray):
        if self.retained(gray) < .98:
            return
        mask = self.ink(cut(gray, self.crop))
        added = int(np.count_nonzero(mask & ~self.base & self.growth_area))
        if added > self.score and added >= max(12, np.count_nonzero(self.old) * .008):
            self.score, self.chosen_at, self.frame = added, seconds, frame

    def capture(self):
        frame = self.frame
        native = np.rot90(frame.to_ndarray(format="bgr24"), (int(frame.rotation or 0) % 360) // 90)
        frame_height, frame_width = native.shape[:2]
        scale = min(1, video_ocr.DETECTOR_SIZE / max(frame.width, frame.height))
        width = max(32, round(frame.width * scale))
        height = max(32, round(frame.height * scale))
        if (int(frame.rotation or 0) // 90) % 2:
            width, height = height, width
        crop = tuple(round(p * factor) for p, factor in zip(
            self.crop, (frame_width / width, frame_height / height) * 2))
        self.pixels = cut(native, crop).copy()
        self.frame = None

    def read(self, engine):
        result = engine(self.pixels)
        lines = [(box, text, float(confidence))
                 for box, text, confidence in zip(
                     result.boxes if result.boxes is not None else [],
                     result.txts if result.txts is not None else [],
                     result.scores if result.scores is not None else [])
                 if confidence >= VIDEO_OCR_CONFIDENCE and text.strip()]
        if not lines:
            return "", 0.0
        boxes, texts, scores = zip(*lines)
        return text_rows(boxes, texts), min(scores)


def new_probes(tracker, seconds):
    gray = tracker.previous
    height, width = gray.shape
    for track in tracker.active:
        if track.first == seconds and track.prefix is not None:
            box = video_ocr.clipped_box(track.polygon, width, height)
            probe = Probe.start_at(seconds, box, gray)
            if probe is not None:
                yield probe


def monitored_samples(reader, duration, tracker, completed, metrics):
    reader._open()
    target, next_probe, probes = 0, 0, []
    for frame in reader.container.decode(reader.stream):
        if frame.time is None:
            continue
        seconds = round(float(frame.time) - reader.origin, 9)
        if seconds >= duration:
            break
        if seconds >= target - 1e-6:
            if any(probe.frame is not None for probe in probes):
                started = perf_counter()
                gray = gray_frame(frame)
                for probe in probes:
                    if probe.frame is not None and probe.retained(gray) <= .50:
                        probe.capture()
                        metrics["retained_crop_bytes"] += probe.pixels.nbytes
                        completed.append(probe)
                metrics["probe_seconds"] += perf_counter() - started
            yield max(0.0, seconds), frame
            started = perf_counter()
            probes = list(new_probes(tracker[0], seconds))
            metrics["triggered"] += len(probes)
            metrics["probe_seconds"] += perf_counter() - started
            target = (math.floor((seconds + 1e-6) / VIDEO_OCR_INTERVAL) + 1) * VIDEO_OCR_INTERVAL
            next_probe = (math.floor((seconds + 1e-6) / STEP) + 1) * STEP
        elif probes and seconds >= next_probe - 1e-6:
            started = perf_counter()
            gray = gray_frame(frame)
            for probe in probes:
                probe.watch(seconds, frame, gray)
            metrics["extra_frames_checked"] += 1
            metrics["probe_seconds"] += perf_counter() - started
            next_probe = (math.floor((seconds + 1e-6) / STEP) + 1) * STEP


def compare_key(text):
    return _normalized(text.replace("!", "！")).casefold()


def select_replacements(records, completed, engine, metrics):
    by_end = {}
    for index, (candidate, _, _) in enumerate(records):
        by_end.setdefault(candidate.last, []).append(index)
    updated = list(records)
    changes = []
    for probe in completed:
        started = perf_counter()
        matches = [index for index in by_end.get(probe.start, [])
                   if video_ocr.overlap(updated[index][0].box, probe.anchor) >= .65]
        if len(matches) != 1:
            metrics["unpaired"] += 1
            continue
        index, = matches
        candidate, old, _ = updated[index]
        if _requires_literal_match(old):
            metrics["protected"] += 1
            continue
        new, confidence = probe.read(engine)
        metrics["extra_ocr_regions"] += 1
        metrics["extra_ocr_seconds"] += perf_counter() - started
        before, after = compare_key(old), compare_key(new)
        if (_requires_literal_match(new) or len(before) < 6 or len(after) <= len(before)
                or not after.startswith(before)):
            metrics["rejected"] += 1
            continue
        updated[index] = replace(candidate, first=probe.chosen_at, last=probe.chosen_at, samples=1), new, confidence
        changes.append({"旧时间": [candidate.first, candidate.last], "新时间": probe.chosen_at,
                        "文字框": probe.anchor, "新增前景像素": probe.score, "旧文字": old, "新文字": new,
                        "新置信度": confidence})
    return updated, changes


def raw_signature(records):
    return sorted((candidate.first, candidate.last, text) for candidate, text, _ in records)


def run(path, duration, engine, enhanced):
    real_tracker = video_ocr.TextRegions
    real_reader = video_ocr.read_candidates
    tracker, completed = [], []
    metrics = dict(triggered=0, extra_frames_checked=0, retained_crop_bytes=0, probe_seconds=0.0, extra_ocr_seconds=0.0,
                   extra_ocr_regions=0, rejected=0, unpaired=0, protected=0)
    details = {}

    def new_tracker(detector, storage):
        result = real_tracker(detector, storage)
        tracker.append(result)
        return result

    def read(engine, storage, candidates, check_stop):
        records, used = real_reader(engine, storage, candidates, check_stop)
        details["raw_before"] = raw_signature(records)
        metrics["original_reads"] = used
        metrics["candidates"] = len(candidates)
        if enhanced:
            records, details["changes"] = select_replacements(records, completed, engine, metrics)
        details["raw_after"] = raw_signature(records)
        return records, used

    reader = UniformFrameReader(path, {})
    samples = (monitored_samples(reader, duration, tracker, completed, metrics)
               if enhanced else reader.samples(duration, VIDEO_OCR_INTERVAL))
    begin = perf_counter()
    try:
        with patch.object(video_ocr, "TextRegions", new_tracker), patch.object(video_ocr, "read_candidates", read):
            rows = video_ocr.recognize_lines(samples, lambda: engine, lambda: None, lambda _: None)
    finally:
        reader.close()
    metrics["wall_seconds"] = round(perf_counter() - begin, 3)
    metrics["reset_candidates"] = len(completed)
    metrics["rows"] = len(rows)
    metrics["characters"] = sum(len(row["text"]) for row in rows)
    return metrics, rows, details


def compare(case, engine):
    path, duration = CASES[case]
    standard, base_rows, base = run(path, duration, engine, False)
    print(case, "baseline", standard, flush=True)
    timed, rows, detail = run(path, duration, engine, True)
    print(case, "probe", timed, flush=True)
    assert base["raw_before"] == detail["raw_before"], "Probe changed normal OCR observations or matching"
    assert standard["original_reads"] == timed["original_reads"]
    protected = [Counter(row for row in items if _requires_literal_match(row[2]))
                 for items in (base["raw_after"], detail["raw_after"])]
    assert protected[0] == protected[1]
    if case == "gemini":
        changes = detail["changes"]
        assert any(row["旧文字"].endswith("委") and row["新文字"].endswith("委屈，")
                   and 54.0 < row["新时间"] < 54.76 for row in changes), changes
    output = {"来源": str(path), "时长秒": duration, "现有": standard, "补帧": timed,
              "所有带数字与运算符的原始记录核对数": sum(protected[0].values()),
              "变化": detail["changes"], "原版OCR": base_rows, "实验OCR": rows}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{case}.json"
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(case, "result", destination, "changes", detail["changes"][:5], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+", choices=tuple(CASES))
    args = parser.parse_args()
    engine = Recognizer(Settings(model_dir=PROJECT / "models"))._video_engine()
    engine._load_det_model()
    engine.cls_and_rotate([np.zeros((28, 112, 3), dtype=np.uint8)])
    engine.recognize_txt([np.zeros((28, 112, 3), dtype=np.uint8)])
    for case in args.cases:
        compare(case, engine)


if __name__ == "__main__":
    main()
