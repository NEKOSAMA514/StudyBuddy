"""跟踪视频文字区域，从每条入选轨迹的清晰裁剪中识别文字。"""
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import zip_longest
import logging
import math
from statistics import quantiles
from tempfile import SpooledTemporaryFile
from time import monotonic
import zlib

import cv2
import numpy as np
from rapidocr.utils.process_img import get_rotate_crop_image

from .extraction import VIDEO_OCR_CONFIDENCE


logger = logging.getLogger("studybuddy")
DETECTOR_SIZE = 640
TILE_SIZE = 32
FULL_REFRESH_SECONDS = 30
WINDOW_SECONDS = 24
OCR_TARGET_SECONDS = 60
MIN_READ_SECONDS = 25
BATCH_SIZE = 12


@dataclass
class Track:
    first: float
    last: float
    polygon: np.ndarray
    signature: np.ndarray
    crop: np.ndarray
    best_box: tuple
    quality: float
    samples: int = 1

    def observe(self, seconds):
        self.last = seconds
        self.samples += 1


@dataclass(frozen=True)
class Candidate:
    first: float
    last: float
    box: tuple
    quality: float
    samples: int
    offset: int
    length: int
    shape: tuple


def resize(image, width, height):
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def alignment(previous, current):
    """只接受能明显降低像素差的平移。"""
    height, width = current.shape
    small_width = min(224, width)
    small_height = max(32, round(small_width * height / width))
    before, after = (resize(image, small_width, small_height) for image in (previous, current))
    if cv2.mean(cv2.absdiff(before, after))[0] < 1.25:
        return 0, 0
    x, y = cv2.phaseCorrelate(before.astype(np.float32), after.astype(np.float32))[0]
    dx, dy = round(x * width / small_width), round(y * height / small_height)
    if (dx == dy == 0) or abs(dx) > width // 10 or abs(dy) > height // 10:
        return 0, 0
    sx, sy = dx * small_width / width, dy * small_height / height
    left, right = max(0, math.ceil(sx)), min(small_width, small_width + math.floor(sx))
    top, bottom = max(0, math.ceil(sy)), min(small_height, small_height + math.floor(sy))
    moved = cv2.warpAffine(before, np.float32([[1, 0, sx], [0, 1, sy]]), (small_width, small_height))
    common = after[top:bottom, left:right]
    moved_error = cv2.mean(cv2.absdiff(moved[top:bottom, left:right], common))[0]
    original_error = cv2.mean(cv2.absdiff(before[top:bottom, left:right], common))[0]
    if moved_error > .72 * original_error:
        return 0, 0
    mx, my = width // 8, height // 8
    template = previous[my:height - my, mx:width - mx]
    x0, y0 = mx + dx - 2, my + dy - 2
    x1, y1 = x0 + template.shape[1] + 4, y0 + template.shape[0] + 4
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return 0, 0
    error = cv2.matchTemplate(current[y0:y1, x0:x1], template, cv2.TM_SQDIFF)
    _, _, (rx, ry), _ = cv2.minMaxLoc(error)
    return dx + rx - 2, dy + ry - 2


def bounds(polygon):
    return (*map(float, polygon.min(axis=0)), *map(float, polygon.max(axis=0)))


def clipped_box(polygon, width, height):
    x, y, right, bottom = bounds(polygon)
    return (max(0, math.floor(x)), max(0, math.floor(y)),
            min(width, math.ceil(right) + 1), min(height, math.ceil(bottom) + 1))


def intersection(left, right):
    return (max(0, min(left[2], right[2]) - max(left[0], right[0])) *
            max(0, min(left[3], right[3]) - max(left[1], right[1])))


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def overlap(left, right):
    shared = intersection(left, right)
    return shared / (area(left) + area(right) - shared) if shared else 0


def merge_rectangles(rectangles):
    merged = []
    for rectangle in rectangles:
        index = 0
        while index < len(merged):
            if not intersection(rectangle, merged[index]):
                index += 1
                continue
            x, y, right, bottom = rectangle
            u, v, end_x, end_y = merged.pop(index)
            rectangle = min(x, u), min(y, v), max(right, end_x), max(bottom, end_y)
            index = 0
        merged.append(rectangle)
    return merged


def integral_sum(integral, x, y, right, bottom):
    return integral[bottom, right] - integral[bottom, x] - integral[y, right] + integral[y, x]


def changed_regions(reference, current, tracks, dx, dy):
    height, width = current.shape
    changed = (cv2.absdiff(reference, current) >= 18).astype(np.uint8)
    coherent = cv2.boxFilter(changed, cv2.CV_16U, (3, 3), normalize=False)
    integral = cv2.integral((changed & (coherent >= 2)).astype(np.uint8))
    x = np.r_[np.arange(0, width, TILE_SIZE), width]
    y = np.r_[np.arange(0, height, TILE_SIZE), height]
    tiles = (integral_sum(integral, x[:-1], y[:-1, None], x[1:], y[1:, None]) >= 7).astype(np.uint8)
    for track in tracks:
        left, top, right, bottom = clipped_box(track.polygon, width, height)
        if right > left and bottom > top and integral_sum(integral, left, top, right, bottom) >= 4:
            tiles[top // TILE_SIZE:(bottom - 1) // TILE_SIZE + 1,
                  left // TILE_SIZE:(right - 1) // TILE_SIZE + 1] = 1
    _, _, stats, _ = cv2.connectedComponentsWithStats(tiles, connectivity=8)
    rectangles = [(max(0, int(x[c]) - 24), max(0, int(y[r]) - 16),
                   min(width, int(x[c + cols]) + 24), min(height, int(y[r + rows]) + 16))
                  for c, r, cols, rows, _ in stats[1:]]
    regions = merge_rectangles(rectangles)
    # 平移后露出的边缘分开检测。
    if dx:
        regions.append((0, 0, min(width, dx + 24), height) if dx > 0
                       else (max(0, width + dx - 24), 0, width, height))
    if dy:
        regions.append((0, 0, width, min(height, dy + 16)) if dy > 0
                       else (0, max(0, height + dy - 16), width, height))
    return regions


def same_line(before, after):
    difference = cv2.absdiff(before, after)
    columns = np.percentile(difference.reshape(24, 10, 12), 85, axis=(0, 2))
    if difference.mean() > 13 or columns.max() > 38:
        return False
    # 局部像素差用于区分仅有个别字符变化的文字。
    changed = (difference >= 33).astype(np.uint8)
    return cv2.boxFilter(changed, cv2.CV_16U, (7, 5), normalize=False).max() < 12


class TextRegions:
    def __init__(self, detector, crops):
        self.detector = detector
        self.crops = crops
        self.active = []
        self.candidates = []
        self.reference = self.previous = None
        self.last_full = -math.inf

    def finish(self, tracks):
        for track in tracks:
            crop = np.ascontiguousarray(track.crop)
            offset = self.crops.tell()
            length = self.crops.write(zlib.compress(crop, level=1))
            self.candidates.append(Candidate(track.first, track.last, track.best_box,
                                             track.quality, track.samples, offset, length, crop.shape))

    def observe(self, seconds, frame):
        turn = (int(frame.rotation or 0) % 360) // 90
        scale = min(1, DETECTOR_SIZE / max(frame.width, frame.height))
        gray = frame.reformat(width=max(32, round(frame.width * scale)),
                              height=max(32, round(frame.height * scale)), format="gray")
        current = np.ascontiguousarray(np.rot90(gray.to_ndarray(), turn))
        height, width = current.shape
        if self.reference is not None and self.reference.shape != current.shape:
            self.finish(self.active)
            self.active.clear()
            self.reference = None
        full = self.reference is None or seconds - self.last_full >= FULL_REFRESH_SECONDS
        if not full:
            dx, dy = alignment(self.previous, current)
            if dx or dy:
                matrix = np.float32([[1, 0, dx], [0, 1, dy]])
                self.reference = cv2.warpAffine(self.reference, matrix, (width, height), flags=cv2.INTER_NEAREST,
                                                borderMode=cv2.BORDER_REPLICATE)
                keep = []
                for track in self.active:
                    track.polygon += np.float32([dx, dy])
                    if intersection(bounds(track.polygon), (0, 0, width, height)):
                        keep.append(track)
                    else:
                        self.finish((track,))
                self.active = keep
            regions = changed_regions(self.reference, current, self.active, dx, dy)
            full = len(regions) > 4 or sum(map(area, regions)) >= .60 * height * width
        self.previous = current
        if full:
            self.reference = current.copy()
            self.last_full = seconds
            regions = [(0, 0, width, height)]
        if regions:
            self.refresh(frame, turn, seconds, current, regions)
        else:
            for track in self.active:
                track.observe(seconds)

    def refresh(self, frame, turn, seconds, current, regions):
        height, width = current.shape
        native = np.ascontiguousarray(np.rot90(frame.to_ndarray(format="bgr24"), turn))
        screen = native if native.shape[:2] == (height, width) else resize(native, width, height)
        scale = np.float32([native.shape[1] / width, native.shape[0] / height])
        keep, previous = [], []
        for track in self.active:
            clipped = clipped_box(track.polygon, width, height)
            if any(intersection(clipped, region) for region in regions):
                previous.append((bounds(track.polygon), track))
            else:
                track.observe(seconds)
                keep.append(track)

        boxes = []
        for left, top, right, bottom in regions:
            detected = self.detector(np.ascontiguousarray(screen[top:bottom, left:right])).boxes
            self.reference[top:bottom, left:right] = current[top:bottom, left:right]
            if detected is not None:
                boxes.extend(detected.astype(np.float32) + np.float32([left, top]))
        added = []
        for polygon in boxes:
            box = bounds(polygon)
            if min(box[2] - box[0], box[3] - box[1]) < 2 or any(overlap(box, other) >= .70 for other in added):
                continue
            crop = get_rotate_crop_image(native, polygon * scale)
            if min(crop.shape[:2]) < 2:
                continue
            added.append(box)
            signature = resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 120, 24)
            quality = float(cv2.Laplacian(signature, cv2.CV_32F).var())
            matches = sorted(((overlap(box, old_box), index) for index, (old_box, _) in enumerate(previous)), reverse=True)
            track = None
            for score, index in matches:
                if score < .65:
                    break
                if not same_line(previous[index][1].signature, signature):
                    continue
                _, track = previous.pop(index)
                track.polygon = polygon
                track.observe(seconds)
                if quality > track.quality:
                    track.crop, track.best_box, track.quality = crop, box, quality
                break
            if track is None:
                track = Track(seconds, seconds, polygon, signature, crop, box, quality)
            keep.append(track)
        self.active = keep
        self.finish(track for _, track in previous)


def spread_windows(windows):
    ordered = sorted(windows)
    ranges = deque([(0, len(ordered))])
    while ranges:
        left, right = ranges.popleft()
        if left < right:
            middle = (left + right) // 2
            yield ordered[middle]
            ranges.extend(((left, middle), (middle + 1, right)))


def ranked_positions(positions):
    def quality(candidate):
        return min(candidate.samples, 5), candidate.quality, -candidate.first

    groups = [sorted(group, key=quality, reverse=True) for group in positions.values()]
    groups.sort(key=lambda group: (len(group), *(-value for value in quality(group[0]))))
    return (candidate for row in zip_longest(*groups) for candidate in row if candidate is not None)


def rank(candidates):
    windows = defaultdict(lambda: (defaultdict(list), defaultdict(list)))
    for candidate in candidates:
        left, top, right, bottom = candidate.box
        position = round((left + right) / 160), round((top + bottom) / 48)
        category = int(candidate.last - candidate.first > 2 + 1e-6)
        windows[int(candidate.first // WINDOW_SECONDS)][category][position].append(candidate)
    queues = deque()
    for window in spread_windows(windows):
        short, long = (ranked_positions(positions) for positions in windows[window])
        queues.append((candidate for pair in zip_longest(short, long) for candidate in pair if candidate is not None))
    while queues:
        queue = queues.popleft()
        candidate = next(queue, None)
        if candidate is not None:
            yield candidate
            queues.append(queue)


def workload(batch, height, width, batch_size):
    ratios = sorted(max(1.0, candidate.shape[1] / candidate.shape[0] * height / width) for candidate in batch)
    groups = (ratios[i:i + batch_size] for i in range(0, len(ratios), batch_size))
    return sum(len(group) * (1 + group[-1]) / 2 for group in groups)


def read_crops(engine, crops, candidates, deadline, check_stop):
    if not candidates:
        return [], 0
    config = engine._load_rec_model()
    _, height, width = config.rec_image_shape
    records, used = [], 0
    timings = deque([.014], maxlen=18)
    while used < len(candidates):
        check_stop()
        remaining = deadline - monotonic() - .25
        cautious = max(.006, quantiles(timings, n=5, method="inclusive")[3] * 1.25) if len(timings) > 1 else .021
        batch = candidates[used:used + BATCH_SIZE]
        while batch:
            cost = workload(batch, height, width, config.rec_batch_num)
            if cost * cautious <= remaining:
                break
            batch.pop()
        if not batch:
            break
        tick = monotonic()
        images = []
        for candidate in batch:
            crops.seek(candidate.offset)
            pixels = zlib.decompress(crops.read(candidate.length))
            images.append(np.frombuffer(pixels, dtype=np.uint8).reshape(candidate.shape).copy())
        images, _ = engine.cls_and_rotate(images)
        result = engine.recognize_txt(images)
        for candidate, text, score in zip(batch, result.txts, result.scores, strict=True):
            if text.strip() and float(score) >= VIDEO_OCR_CONFIDENCE:
                records.append((candidate, text.strip(), float(score)))
        timings.append((monotonic() - tick) / cost)
        used += len(batch)
    return records, used


def evidence_rows(records):
    grouped = defaultdict(list)
    for candidate, text, confidence in records:
        grouped[candidate.first].append((candidate, text, confidence))
    rows = []
    for seconds, lines in sorted(grouped.items()):
        lines.sort(key=lambda line: (line[0].box[1], line[0].box[0]))
        rows.append({"start_seconds": seconds, "end_seconds": max(line[0].last for line in lines),
                     "text": "\n".join(line[1] for line in lines),
                     "confidence": 100 * sum(line[2] for line in lines) / len(lines)})
    return rows


def recognize_lines(samples, engine_factory, check_stop, on_sample):
    start = checked = monotonic()
    engine = engine_factory()
    # 已结束轨迹的裁剪无损压缩，超过内存额度后转入临时文件。
    with SpooledTemporaryFile(max_size=64 * 1024**2) as crops:
        tracker = TextRegions(engine._load_det_model(), crops)
        count = 0
        for seconds, frame in samples:
            if monotonic() - checked >= .5:
                check_stop()
                checked = monotonic()
            tracker.observe(seconds, frame)
            count += 1
            on_sample(seconds)
        check_stop()
        tracker.finish(tracker.active)
        tracker.active.clear()
        candidates = list(rank(tracker.candidates))
        deadline = max(start + OCR_TARGET_SECONDS, monotonic() + MIN_READ_SECONDS)
        records, used = read_crops(engine, crops, candidates, deadline, check_stop)
    logger.info("video_ocr samples=%s candidates=%s read=%s retained=%s seconds=%.2f",
                count, len(candidates), used, len(records), monotonic() - start)
    return evidence_rows(records)
