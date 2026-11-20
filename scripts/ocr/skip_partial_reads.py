"""Test choosing a richer frame before dispatching older crops to Small."""
import argparse
from collections import Counter, deque
from dataclasses import replace
import json
import math
from time import perf_counter
import unicodedata
from unittest.mock import patch
import zlib

import cv2
import numpy as np

import growth_frames as growth

ocr = growth.video_ocr
OUTPUT = growth.PROJECT / "test-results" / "ocr-preread-skip-trial"
RECENT_SAMPLES = 16
CASES = {**growth.CASES, "parenting": (growth.CASES["gemini"][0], 1605.137583)}


def protected(text):
    return growth._requires_literal_match(text) or any(unicodedata.category(ch) in {"Sm", "Sc"} for ch in text)


def same_paragraph(box, peer):
    left, top, right, bottom = box
    x0, y0, x1, y1 = peer
    h = min(bottom - top, y1 - y0)
    if h < .72 * max(bottom - top, y1 - y0):
        return False
    same_row = (min(bottom, y1) - max(top, y0) >= .7 * h and max(left, x0) - min(right, x1) <= .4 * h)
    next_row = (abs(left - x0) <= .25 * h and min(right, x1) - max(left, x0) >= 2.5 * h
                and abs(max(top, y0) - min(bottom, y1)) <= .28 * h)
    return same_row or next_row


class PageProbe(growth.Probe):
    def capture(self):
        left, top, right, bottom = self.crop
        gray = growth.gray_frame(self.frame)
        height, width = gray.shape
        self.crop = (max(0, min([left, *[b[0]-4 for b in self.page_boxes]])),
                     max(0, min([top, *[b[1]-4 for b in self.page_boxes]])),
                     min(width, max([right, *[b[2]+4 for b in self.page_boxes]])),
                     min(height, max([bottom, *[b[3]+4 for b in self.page_boxes]])))
        self.observations = [(t, growth.cut(frame, self.crop).copy()) for t, frame in self.history
                             if frame.shape == gray.shape]
        self.chosen_gray = growth.cut(gray, self.crop).copy()
        super().capture()


def eligible_shapes(probe, candidate, recognized_area):
    """Prove that each earlier glyph remains in the portion of the winning frame to be read."""
    if not probe.observations or candidate.first < probe.observations[0][0] or candidate.last > probe.start:
        return False
    left, top, right, bottom = (math.floor(candidate.box[0]), math.floor(candidate.box[1]),
                                math.ceil(candidate.box[2]), math.ceil(candidate.box[3]))
    if not ocr.covers(probe.crop, (left, top, right, bottom)) or right-left < 2 or bottom-top < 2:
        return False
    offset_x, offset_y = probe.crop[:2]
    box = (left-offset_x, top-offset_y, right-offset_x, bottom-offset_y)
    samples = [growth.cut(image, box) for t, image in probe.observations if t >= candidate.first]
    if not samples:
        return False
    level, bright = cv2.threshold(samples[0], 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    light = np.count_nonzero(bright) < bright.size / 2
    old = bright > 0 if light else bright == 0
    if np.count_nonzero(old) < 16:
        return False
    # Only compare within the original glyph extent. Ink appended to the right remains outside it.
    x0, y0, w, h = cv2.boundingRect(old.astype(np.uint8))
    tight = (x0, y0, x0 + w, y0 + h)
    old = growth.cut(old, tight)
    if np.count_nonzero(old & ~growth.cut(growth.cut(recognized_area, box), tight)) > 2:
        return False
    near_old = cv2.dilate(old.astype(np.uint8), growth.KERNEL).astype(bool)
    for sample in [*samples[1:], growth.cut(probe.chosen_gray, box)]:
        sample = growth.cut(sample, tight)
        new = sample > level if light else sample <= level
        near_new = cv2.dilate(new.astype(np.uint8), growth.KERNEL).astype(bool)
        edits = ((old & ~near_new) | (new & ~near_old)).astype(np.uint8)
        if np.any(edits) and cv2.boxFilter(edits, cv2.CV_16U, (12, 12), normalize=False).max() > 3:
            return False
    return True


def recognized_frame(engine, probe, candidates, selected, metrics):
    possible = [item for item in candidates if id(item) not in selected and item.alternate is None and
                probe.observations and probe.observations[0][0] <= item.first and item.last <= probe.start and
                ocr.covers(probe.crop, item.box)]
    if len(possible) < 2:
        return [], []
    height, width = probe.chosen_gray.shape
    screen = cv2.resize(probe.pixels, (width, height), interpolation=cv2.INTER_AREA)
    detected = engine._load_det_model()(screen).boxes
    metrics["winner_detections"] += 1
    if detected is None:
        return [], []
    native_h, native_w = probe.pixels.shape[:2]
    ratio = np.asarray((native_w / width, native_h / height), dtype=np.float32)
    boxes, images = [], []
    for polygon in detected:
        image = ocr.get_rotate_crop_image(probe.pixels, polygon.astype(np.float32) * ratio)
        if min(image.shape[:2]) >= 2:
            boxes.append(polygon)
            images.append(image)
    if not boxes or len(boxes) >= len(possible):
        return [], []
    area = np.zeros((height, width), dtype=np.uint8)
    for polygon in boxes:
        cv2.fillConvexPoly(area, polygon.astype(np.int32), 1)
    area = cv2.dilate(area, growth.KERNEL).astype(bool)
    covered = [item for item in possible if eligible_shapes(probe, item, area)]
    if len(covered) <= len(images):
        return [], []
    images, _ = engine.cls_and_rotate(images)
    result = engine.recognize_txt(images)
    metrics["winner_small_crops"] += len(images)
    recognized = list(zip(boxes, result.txts, result.scores, strict=True))
    if any(float(score) < growth.VIDEO_OCR_CONFIDENCE or len(growth.compare_key(text)) < 6 or protected(text)
           for _, text, score in recognized):
        metrics["fallbacks"] += 1
        return [], []
    offset = np.asarray(probe.crop[:2], dtype=np.float32)
    records = [(replace(covered[0], first=probe.chosen_at, last=probe.chosen_at, samples=1,
                        box=ocr.bounds(polygon + offset), alternate=None, prefix=None), text, float(score))
               for polygon, text, score in recognized]
    return covered, records


def read_preserving_padding(engine, storage, candidates, check_stop, omitted, covered):
    """Keep Small's aspect-ratio groups and padding while removing entries within each group."""
    recognizer = engine._load_rec_model()
    normalize = recognizer.resize_norm_img
    _, nominal_height, nominal_width = recognizer.rec_image_shape
    pending, records, used = deque(candidates), [], 0
    while pending:
        check_stop()
        batch = [pending.popleft() for _ in range(min(ocr.BATCH_SIZE, len(pending)))]
        planned = [candidate for candidate in batch if id(candidate) not in omitted]
        active = [candidate for candidate in planned if id(candidate) not in covered]
        if not active:
            continue
        images = []
        for candidate in active:
            storage.seek(candidate.offset)
            pixels = zlib.decompress(storage.read(candidate.length))
            images.append(np.frombuffer(pixels, dtype=np.uint8).reshape(candidate.shape).copy())
        images, _ = engine.cls_and_rotate(images)
        if len(active) == len(planned):
            result = engine.recognize_txt(images)
            by_id = {id(candidate): (text, score)
                     for candidate, text, score in zip(active, result.txts, result.scores, strict=True)}
        else:
            ratios = np.asarray([candidate.shape[1] / candidate.shape[0] for candidate in planned])
            order = np.argsort(ratios)
            images_by_id = {id(candidate): image for candidate, image in zip(active, images, strict=True)}
            by_id = {}
            for begin in range(0, len(order), recognizer.rec_batch_num):
                group = order[begin:begin + recognizer.rec_batch_num]
                ids = [id(planned[index]) for index in group if id(planned[index]) in images_by_id]
                if not ids:
                    continue
                width = max(nominal_width / nominal_height, float(ratios[group[-1]]))
                with patch.object(recognizer, "resize_norm_img", lambda image, _: normalize(image, width)):
                    result = engine.recognize_txt([images_by_id[i] for i in ids])
                by_id.update({i: (text, score) for i, text, score in zip(ids, result.txts, result.scores, strict=True)})
        retries = []
        for candidate in active:
            text, score = by_id[id(candidate)]
            if text.strip() and float(score) >= growth.VIDEO_OCR_CONFIDENCE:
                records.append((candidate, text.strip(), float(score)))
            elif candidate.alternate is not None:
                retries.append(candidate.alternate)
        used += len(active)
        pending.extendleft(reversed(retries))
    return records, used


def run_prefilter(path, duration, engine):
    actual_tracker = ocr.TextRegions
    actual_reader = ocr.read_candidates
    actual_crops = ocr.read_crops
    triggers = growth.new_probes
    page_history = deque(maxlen=RECENT_SAMPLES)
    tracker, completed, groups = [], [], []
    metrics = dict(probe_seconds=0.0, triggered=0, extra_frames_checked=0, retained_crop_bytes=0,
                   winner_detections=0, winner_small_crops=0, fallback_crops=0, fallbacks=0, skipped_candidates=0)
    state = {}

    def new_tracker(detector, storage):
        result = actual_tracker(detector, storage)
        tracker.append(result)
        return result

    def new_probes(tracker, seconds):
        gray = tracker.previous
        page_history.append((seconds, gray))
        active = None
        for probe in triggers(tracker, seconds):
            if active is None:
                height, width = gray.shape
                active = [ocr.clipped_box(item.polygon, width, height) for item in tracker.active]
            page = [probe.anchor]
            for box in page:
                page.extend(peer for peer in active if peer not in page and same_paragraph(box, peer))
            probe.page_boxes, probe.history = page, page_history
            yield probe

    def prefilter(engine, storage, candidates, check_stop):
        started = perf_counter()
        rich, skipped = [], set()
        parents = {id(candidate.prefix) for candidate in candidates if candidate.prefix is not None}
        legacy_terminals = {id(candidate) for candidate in candidates if id(candidate) not in parents
                            and candidate.samples >= 2 and candidate.prefix is not None and candidate.prefix.prefix is not None}
        for probe in completed:
            covered, records = recognized_frame(engine, probe, candidates, skipped | legacy_terminals, metrics)
            if records:
                skipped.update(id(item) for item in covered)
                rich.extend(records)
                ordered = sorted(records, key=lambda row: (row[0].box[1], row[0].box[0]))
                groups.append({"代表帧秒": probe.chosen_at,
                               "跳过": [list(identity(item)) for item in covered],
                               "文字": "\n".join(text for _, text, _ in ordered)})
        metrics["selection_seconds"] = perf_counter() - started
        def read_with_skips(engine, storage, batch, check_stop, omitted=frozenset()):
            if any(id(item) in skipped and id(item) not in omitted for item in batch):
                return read_preserving_padding(engine, storage, batch, check_stop, omitted, skipped)
            return actual_crops(engine, storage, batch, check_stop, omitted)

        with patch.object(ocr, "read_crops", read_with_skips):
            remaining, used = actual_reader(engine, storage, candidates, check_stop)
        metrics["fallback_crops"] = used
        metrics["skipped_candidates"] = len(skipped)
        state["raw"] = [(*identity(candidate), text) for candidate, text, _ in [*remaining, *rich]]
        return [*remaining, *rich], used + metrics["winner_small_crops"]

    reader = growth.UniformFrameReader(path, {})
    started = perf_counter()
    try:
        with (patch.object(ocr, "TextRegions", new_tracker), patch.object(ocr, "read_candidates", prefilter),
              patch.object(growth, "Probe", PageProbe), patch.object(growth, "new_probes", new_probes)):
            rows = ocr.recognize_lines(growth.monitored_samples(reader, duration, tracker, completed, metrics),
                                       lambda: engine, lambda: None, lambda _: None)
    finally:
        reader.close()
    metrics["wall_seconds"] = round(perf_counter() - started, 3)
    metrics["reset_candidates"] = len(completed)
    metrics["small_crops"] = metrics["fallback_crops"] + metrics["winner_small_crops"]
    return metrics, rows, state["raw"], groups


def identity(candidate):
    return candidate.first, candidate.last, tuple(float(x) for x in candidate.box)


def run_baseline(path, duration, engine):
    actual_reader = ocr.read_candidates
    raw = []
    def read(engine, storage, candidates, check_stop):
        records, used = actual_reader(engine, storage, candidates, check_stop)
        raw.extend((*identity(candidate), text) for candidate, text, _ in records)
        return records, used
    with patch.object(ocr, "read_candidates", read):
        metrics, rows, _ = growth.run(path, duration, engine, False)
    return metrics, rows, raw


def audit(base, filtered, groups):
    evidence = {tuple(row): growth.compare_key(group["文字"]) for group in groups for row in group["跳过"]}
    missing = Counter(base) - Counter(filtered)
    lost = []
    for item, count in missing.items():
        key, text = item[:3], item[3]
        if protected(text) or growth.compare_key(text) not in evidence.get(key, ""):
            lost.extend([item] * count)
    return sum(missing.values()), lost


def compare(case, engine):
    path, duration = CASES[case]
    cache = growth.OUTPUT / "lecture.json"
    cached = case == "lecture" and cache.is_file()
    if cached:
        before = json.loads(cache.read_text(encoding="utf-8"))
        base, old, baseline_raw = before["现有"], before["原版OCR"], None
    else:
        base, old, baseline_raw = run_baseline(path, duration, engine)
    print(case, "baseline", base["original_reads"], base["wall_seconds"], flush=True)
    metrics, rows, raw, groups = run_prefilter(path, duration, engine)
    print(case, "prefilter", metrics, flush=True)
    removed, lost = audit(baseline_raw, raw, groups) if baseline_raw is not None else (0, [])
    if cached:
        assert old == rows, "The lecture with a cached baseline needs an independent audit of any changed evidence."
    result = {"来源": str(path), "范围秒": duration, "基线复用": cached, "现有": base, "读前筛选": metrics,
              "已覆盖的旧识别行": removed - len(lost), "待核对的旧识别": lost, "合并组": groups,
              "原版OCR": old, "实验OCR": rows}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    out = OUTPUT / f"{case}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(case, "audit", len(groups), removed, len(lost), str(out), flush=True)
    for group in groups[:3]:
        print(group["代表帧秒"], len(group["跳过"]), repr(group["文字"]), flush=True)
    if lost:
        print("uncovered samples", lost[:12], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+", choices=tuple(CASES))
    options = parser.parse_args()
    engine = growth.Recognizer(growth.Settings(model_dir=growth.PROJECT / "models"))._video_engine()
    engine._load_det_model()
    engine.cls_and_rotate([np.zeros((28, 112, 3), dtype=np.uint8)])
    engine.recognize_txt([np.zeros((28, 112, 3), dtype=np.uint8)])
    for case in options.cases:
        compare(case, engine)


if __name__ == "__main__":
    main()
