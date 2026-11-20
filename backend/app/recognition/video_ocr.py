"""跟踪视频文字区域，从每条轨迹的清晰裁剪中识别文字。"""
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from copy import copy
from dataclasses import dataclass
from itertools import zip_longest
import logging
import math
from tempfile import SpooledTemporaryFile
from time import monotonic
import unicodedata
import zlib

import cv2
import numpy as np
from rapidocr.ch_ppocr_rec.typings import TextRecInput
from rapidocr.utils.process_img import get_rotate_crop_image

from .extraction import VIDEO_OCR_CONFIDENCE
from .ocr_text import FORMULA_SYMBOLS, OCR_SYMBOLS, requires_literal_match


logger = logging.getLogger("studybuddy")
DETECTOR_SIZE = 640
TILE_SIZE = 32
BOX_JITTER_PIXELS = 3
FULL_REFRESH_SECONDS = 30
WINDOW_SECONDS = 24
BATCH_SIZE = 12


@dataclass(frozen=True)
class LineCrop:
    image: np.ndarray
    box: tuple
    quality: float


@dataclass
class Track:
    first: float
    last: float
    polygon: np.ndarray
    signature: np.ndarray
    best: LineCrop
    samples: int = 1
    alternate: LineCrop | None = None
    prefix: "Candidate | None" = None

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
    alternate: "Candidate | None" = None
    prefix: "Candidate | None" = None


def resize(image, width, height):
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def line_crop(image, points):
    # 水平整数框的透视变换只是平移；复制裁剪，避免候选一直引用整帧。
    (left, top), (right, top_right_y), (bottom_right_x, bottom), (bottom_left_x, bottom_left_y) = points
    if (left == bottom_left_x and right == bottom_right_x and top == top_right_y and bottom == bottom_left_y and
            0 <= left < right <= image.shape[1] and 0 <= top < bottom <= image.shape[0] and
            all(value.is_integer() for value in (left, right, top, bottom))):
        crop = image[int(top):int(bottom), int(left):int(right)].copy()
        return np.rot90(crop) if crop.shape[0] >= 1.5 * crop.shape[1] else crop
    return get_rotate_crop_image(image, points)


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


def covers(outer, inner, margin=0):
    left, top, right, bottom = outer
    return (left - margin <= inner[0] and top - margin <= inner[1] and
            right + margin >= inner[2] and bottom + margin >= inner[3])


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
    _, changed = cv2.threshold(cv2.absdiff(reference, current), 17, 1, cv2.THRESH_BINARY)
    if not (dx or dy) and cv2.countNonZero(changed) == 0:
        return []
    coherent = cv2.boxFilter(changed, cv2.CV_16U, (3, 3), normalize=False)
    integral = cv2.integral((changed & (coherent >= 2)).astype(np.uint8))
    x = np.r_[np.arange(0, width, TILE_SIZE), width]
    y = np.r_[np.arange(0, height, TILE_SIZE), height]
    tiles = (integral_sum(integral, x[:-1], y[:-1, None], x[1:], y[1:, None]) >= 7).astype(np.uint8)
    for track in tracks:
        left, top, right, bottom = clipped_box(track.polygon, width, height)
        if right > left and bottom > top and integral_sum(integral, left, top, right, bottom) >= 2:
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


def line_signature(crop):
    return resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), 240, 24)


def same_line(before, after):
    difference = cv2.absdiff(before, after)
    if cv2.mean(difference)[0] > 13:
        return False
    # 每个 7×5 邻域最多允许 2 个差异像素，比 24×24 分栏的 85 分位检查更严格。
    _, changed = cv2.threshold(difference, 32, 1, cv2.THRESH_BINARY)
    if cv2.countNonZero(changed) == 0:
        return True
    return cv2.boxFilter(changed, cv2.CV_16U, (7, 5), normalize=False).max() < 3


def extends_right(old_box, box, before, after):
    left, top, right, bottom = old_box
    if (max(abs(box[0] - left), abs(box[1] - top), abs(box[3] - bottom)) > BOX_JITTER_PIXELS
            or box[2] - right < (bottom - top) * .5
            or intersection(old_box, box) < .85 * area(old_box)):
        return False
    height, width = after.shape
    x0, x1 = max(0, math.ceil(right)), min(width, math.ceil(box[2]))
    y0, y1 = max(0, math.floor(box[1])), min(height, math.ceil(box[3]) + 1)
    if x1 <= x0 or y1 <= y0:
        return False
    changed = cv2.absdiff(before[y0:y1, x0:x1], after[y0:y1, x0:x1]) >= 33
    return np.count_nonzero(changed) >= max(12, changed.size * .04)


def prefix_ink_unchanged(polygon, before, after):
    height, width = before.shape
    left, top, right, bottom = clipped_box(polygon, width, height)
    if right - left < 10 or bottom - top < 5:
        return False
    old, new = (image[top:bottom, left:right] for image in (before, after))
    threshold, _ = cv2.threshold(old, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink, new_ink = old > threshold, new > threshold
    if ink.mean() > .5:
        ink, new_ink = ~ink, ~new_ink
    if not .04 <= ink.mean() <= .42 or np.count_nonzero(ink) < 12:
        return False
    if abs(old[ink].mean() - old[~ink].mean()) < 55:
        return False
    # 只忽略最右笔画以外的留白，字内新增或消失的笔画都要比较。
    end = np.flatnonzero(ink.any(axis=0))[-1] + 1
    changes = (ink[:, :end] != new_ink[:, :end]) & (cv2.absdiff(old[:, :end], new[:, :end]) >= 24)
    return np.count_nonzero(changes) <= 2


class TextRegions:
    def __init__(self, detector, crops):
        self.detector = detector
        self.crops = crops
        self.active = []
        self.candidates = []
        self.reference = self.previous = None
        self.last_full = -math.inf

    def finish(self, tracks):
        def store(track, view, alternate=None, prefix=None):
            crop = np.ascontiguousarray(view.image)
            offset = self.crops.tell()
            length = self.crops.write(zlib.compress(crop, level=1))
            return Candidate(track.first, track.last, view.box, view.quality,
                             track.samples, offset, length, crop.shape, alternate, prefix)

        finished = []
        for track in tracks:
            alternate = store(track, track.alternate) if track.alternate is not None else None
            finished.append(store(track, track.best, alternate, track.prefix))
        self.candidates.extend(finished)
        return finished

    def observe(self, seconds, frame):
        turn = (int(frame.rotation or 0) % 360) // 90
        scale = min(1, DETECTOR_SIZE / max(frame.width, frame.height))
        gray = frame.reformat(width=max(32, round(frame.width * scale)),
                              height=max(32, round(frame.height * scale)), format="gray")
        current = np.ascontiguousarray(np.rot90(gray.to_ndarray(), turn))
        height, width = current.shape
        last_frame = self.previous
        if self.reference is not None and self.reference.shape != current.shape:
            self.finish(self.active)
            self.active.clear()
            self.reference = None
            last_frame = None
        full = self.reference is None or seconds - self.last_full >= FULL_REFRESH_SECONDS
        if not full:
            dx, dy = alignment(self.previous, current)
            if dx or dy:
                last_frame = None
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
            self.refresh(frame, turn, seconds, current, regions, last_frame)
        else:
            for track in self.active:
                track.observe(seconds)

    def refresh(self, frame, turn, seconds, current, regions, last_frame=None):
        height, width = current.shape
        native = np.ascontiguousarray(np.rot90(frame.to_ndarray(format="bgr24"), turn))
        screen = native if native.shape[:2] == (height, width) else resize(native, width, height)
        scale = np.float32([native.shape[1] / width, native.shape[0] / height])
        checked = {}

        def still_visible(track):
            key = id(track)
            if key not in checked:
                crop = line_crop(native, track.polygon * scale)
                checked[key] = min(crop.shape[:2]) >= 2 and same_line(track.signature, line_signature(crop))
            return checked[key]

        # 局部变化可能只擦到旧框的一侧。整行确有变化时，让检测区域覆盖完整旧框。
        active = [(clipped_box(track.polygon, width, height), track) for track in self.active]
        extended = []
        for box, track in active:
            if (any(intersection(box, region) for region in regions) and
                    not any(covers(region, box) for region in regions) and not still_visible(track)):
                left, top, right, bottom = box
                extended.append((max(0, left - 24), max(0, top - 16), min(width, right + 24), min(height, bottom + 16)))
        if extended:
            regions = merge_rectangles([*regions, *extended])

        keep, previous = [], []
        for box, track in active:
            if any(intersection(box, region) for region in regions):
                previous.append((bounds(track.polygon), track))
            else:
                track.observe(seconds)
                keep.append(track)

        boxes = []
        for region in regions:
            left, top, right, bottom = region
            detected = self.detector(np.ascontiguousarray(screen[top:bottom, left:right])).boxes
            self.reference[top:bottom, left:right] = current[top:bottom, left:right]
            if detected is None:
                continue
            overlapping = [(old, track) for old, track in previous if intersection(old, region)]
            for polygon in detected.astype(np.float32) + np.float32([left, top]):
                box = bounds(polygon)
                width = box[2] - box[0]
                # 整行仍可见时，过滤局部截断框和全覆盖重检返回的同行短框。
                if any(covers(old, box, BOX_JITTER_PIXELS)
                       and (not covers(region, old) or (
                           old[2] - old[0] - width > 2 * BOX_JITTER_PIXELS
                           and old[0] <= box[0] and box[2] <= old[2]
                           and max(abs(box[1] - old[1]), abs(box[3] - old[3])) <= BOX_JITTER_PIXELS))
                       and still_visible(track) for old, track in overlapping):
                    continue
                boxes.append((box, polygon))
        added, started = [], []
        for box, polygon in boxes:
            if min(box[2] - box[0], box[3] - box[1]) < 2 or any(overlap(box, other) >= .70 for other in added):
                continue
            crop = line_crop(native, polygon * scale)
            if min(crop.shape[:2]) < 2:
                continue
            added.append(box)
            signature = line_signature(crop)
            quality = float(cv2.Laplacian(signature, cv2.CV_32F).var())
            matches = sorted(((overlap(box, old_box), index) for index, (old_box, _) in enumerate(previous)), reverse=True)
            track = None
            for score, index in matches:
                if score < .65:
                    break
                old_box, old = previous[index]
                if same_line(old.signature, signature):
                    old.polygon = polygon
                    if quality > old.best.quality:
                        old.best = LineCrop(crop, box, quality)
                # 重新裁剪和缩放会放大框边界的轻微抖动。复核成功时沿用原框。
                elif max(abs(a - b) for a, b in zip(box, old_box)) > BOX_JITTER_PIXELS or not still_visible(old):
                    continue
                elif old.alternate is None or quality > old.alternate.quality:
                    # 清晰度不总等于识别置信度。保留一份不同的裁剪用于低置信重读。
                    old.alternate = LineCrop(crop, box, quality)
                _, track = previous.pop(index)
                track.observe(seconds)
                break
            if track is None:
                track = Track(seconds, seconds, polygon, signature, LineCrop(crop, box, quality))
                started.append(track)
            keep.append(track)
        ended = []
        for box, track in previous:
            replaced = any(overlap(box, new) >= .65 or covers(new, box, BOX_JITTER_PIXELS) for new in added)
            if not replaced and still_visible(track):
                track.observe(seconds)
                keep.append(track)
            else:
                ended.append((box, track))
        self.active = keep
        finished = self.finish(track for _, track in ended)
        if last_frame is None or not started or not ended:
            return

        # 先按旧框覆盖率配对，再比较原字形。只连接相邻采样中一对一的追加。
        previous_crops = list(zip(ended, finished, strict=True))
        links = []
        children = Counter()
        last_sample = int(seconds + 1e-6) - 1
        for track in started:
            matches = [candidate for (box, old), candidate in previous_crops
                       if int(old.last + 1e-6) == last_sample
                       and extends_right(box, track.best.box, last_frame, current)
                       and (still_visible(old) or prefix_ink_unchanged(old.polygon, last_frame, current))]
            children.update(id(candidate) for candidate in matches)
            if len(matches) == 1:
                links.append((track, matches[0]))
        for track, candidate in links:
            if children[id(candidate)] == 1:
                track.prefix = candidate


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
        # 持续显示的行可以代表沿途窗口，避免续轨后只挤在首次出现的窗口。
        for window in range(int(candidate.first // WINDOW_SECONDS), int(candidate.last // WINDOW_SECONDS) + 1):
            windows[window][category][position].append(candidate)
    queues = deque()
    for window in spread_windows(windows):
        short, long = (ranked_positions(positions) for positions in windows[window])
        queues.append((candidate for pair in zip_longest(short, long) for candidate in pair if candidate is not None))
    seen = set()
    while queues:
        queue = queues.popleft()
        for candidate in queue:
            identity = id(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            yield candidate
            queues.append(queue)
            break


def recognize_crop_batch(engine, batch, active, images):
    if len(active) == len(batch):
        result = engine.recognize_txt(images)
        return zip(active, result.txts, result.scores, strict=True)

    # Small 按宽高比分组，用同组最宽裁剪决定补宽。筛掉图像时保留原分组的输入宽度。
    model = engine._load_rec_model()
    channels, height, nominal_width = model.rec_image_shape
    ratios = np.array([candidate.shape[1] / candidate.shape[0] for candidate in batch])
    order = np.argsort(ratios)
    by_id = {id(candidate): image for candidate, image in zip(active, images, strict=True)}
    recognized = {}
    for start in range(0, len(order), model.rec_batch_num):
        group = order[start:start + model.rec_batch_num]
        kept = [index for index in group if id(batch[index]) in by_id]
        if not kept:
            continue
        group_images = [by_id[id(batch[index])] for index in kept]
        width = max(nominal_width, int(height * ratios[group[-1]]))
        if width > max(nominal_width, int(height * ratios[kept[-1]])):
            # 只复制识别器的预处理参数，推理会话仍然共享。
            padded = copy(model)
            padded.rec_image_shape = (channels, height, width)
            result = padded(TextRecInput(img=group_images))
        else:
            result = engine.recognize_txt(group_images)
        for index, text, score in zip(kept, result.txts, result.scores, strict=True):
            recognized[id(batch[index])] = (text, score)
    return ((candidate, *recognized[id(candidate)]) for candidate in active)


def read_crops(engine, crops, candidates, check_stop, skipped=frozenset()):
    records, used = [], 0
    pending = deque(candidates)
    while pending:
        check_stop()
        batch = [pending.popleft() for _ in range(min(BATCH_SIZE, len(pending)))]
        active = [candidate for candidate in batch if id(candidate) not in skipped]
        if not active:
            continue
        images = []
        for candidate in active:
            crops.seek(candidate.offset)
            pixels = zlib.decompress(crops.read(candidate.length))
            images.append(np.frombuffer(pixels, dtype=np.uint8).reshape(candidate.shape).copy())
        images, _ = engine.cls_and_rotate(images)
        retries = []
        for candidate, text, score in recognize_crop_batch(engine, batch, active, images):
            if text.strip() and float(score) >= VIDEO_OCR_CONFIDENCE:
                records.append((candidate, text.strip(), float(score)))
            elif candidate.alternate is not None:
                retries.append(candidate.alternate)
        used += len(active)
        pending.extendleft(reversed(retries))
    return records, used


def _can_skip_caption_prefixes(text):
    if len(text) < 6 or not any(char in "，。！？；：、…,.!?;:" for char in text):
        return False
    normalized = text.translate(OCR_SYMBOLS)
    if requires_literal_match(normalized):
        return False
    return not any(char.isnumeric() or unicodedata.category(char) in ("Sm", "Sc") for char in normalized)


def read_candidates(engine, crops, candidates, check_stop):
    def short_lived_line(candidate):
        left, top, right, bottom = candidate.box
        return candidate.last - candidate.first <= 1.05 and right - left >= 2.8 * (bottom - top)

    parents = {id(candidate.prefix) for candidate in candidates if candidate.prefix is not None}
    growing = [candidate for candidate in candidates if id(candidate) not in parents
               and candidate.prefix is not None and short_lived_line(candidate.prefix)]
    if not growing:
        return read_crops(engine, crops, candidates, check_stop)

    terminals = {id(candidate) for candidate in growing}
    preferred, used = read_crops(engine, crops, candidates, check_stop,
                                 {id(candidate) for candidate in candidates if id(candidate) not in terminals})
    accepted = {id(record[0]): record for record in preferred}
    selected, skipped = [], set()
    for candidate in growing:
        record = accepted.get(id(candidate)) or accepted.get(id(candidate.alternate))
        if record is None:
            continue
        selected.append(record)
        skipped.add(id(candidate))
        if not _can_skip_caption_prefixes(record[1]):
            continue
        candidate = candidate.prefix
        while candidate is not None and short_lived_line(candidate):
            skipped.add(id(candidate))
            candidate = candidate.prefix

    records, remaining = read_crops(engine, crops, candidates, check_stop, skipped)
    return [*records, *selected], used + remaining


def _same_baseline(left, right):
    overlap_height = min(left[3], right[3]) - max(left[1], right[1])
    heights = left[3] - left[1], right[3] - right[1]
    return (overlap_height >= .65 * min(heights)
            and abs(left[1] + left[3] - right[1] - right[3]) <= .7 * max(heights))


def _same_line_extent(left, right):
    smaller, larger = sorted((left[2] - left[0], right[2] - right[0]))
    return (smaller >= .73 * larger and min(left[2], right[2]) - max(left[0], right[0]) >= .85 * smaller
            and _same_baseline(left, right))


def _literal_character(char):
    char = char.translate(OCR_SYMBOLS)
    return (char.isnumeric() or (char.isascii() and char.isalpha())
            or char in ".," + FORMULA_SYMBOLS or unicodedata.category(char) == "Sm")


def _fragment_offset(whole, part):
    offset = whole.find(part)
    if offset < 0 or len(whole) - len(part) < 2 or whole.find(part, offset + 1) >= 0:
        return None
    end = offset + len(part)
    # 小数、单位和公式需要完整保留，不能从字母或符号的中间截断。
    if (offset and _literal_character(whole[offset - 1]) and _literal_character(part[0])
            or end < len(whole) and _literal_character(part[-1]) and _literal_character(whole[end])):
        return None
    return offset


def _box_fits_fragment(box, whole, offset, length, total):
    left, _, right, _ = whole
    width = right - left
    tolerance = .13 * width + BOX_JITTER_PIXELS
    return (box[2] - box[0] < .94 * width and _same_baseline(box, whole)
            and left - 4 <= box[0] and box[2] <= right + 4
            and abs(box[0] - (left + width * offset / total)) <= tolerance
            and abs(box[2] - (left + width * (offset + length) / total)) <= tolerance)


def _clipped_line_sources(groups):
    punctuation = str.maketrans("（）：，", "():,")
    lines = []
    for row, group in enumerate(groups):
        line = 0
        for candidate, text, _ in group:
            if len(text) >= 5 and "\n" not in text:
                matching = text.translate(punctuation) if any("\u3400" <= char <= "\u9fff" for char in text) else text
                lines.append((row, line, candidate, matching))
            line += text.count("\n") + 1
    starts = [candidate.first for _, _, candidate, _ in lines]
    complete = defaultdict(list)
    matches = defaultdict(dict)
    for index, (after, _, following, whole) in enumerate(lines):
        if len(whole) < 12:
            continue
        nearby = complete[whole]
        nearby[:] = [(row, candidate) for row, candidate in nearby if candidate.last >= following.first - 30]
        earlier = max(((row, candidate) for row, candidate in nearby if candidate.last < following.first
                       and _same_line_extent(candidate.box, following.box)),
                      key=lambda entry: entry[1].last, default=None)
        nearby.append((after, following))
        if earlier is None:
            continue
        before, preceding = earlier
        end = preceding.last
        supported = []
        for row, line, candidate, part in lines[bisect_right(starts, end):index]:
            if candidate.last >= following.first:
                continue
            offset = _fragment_offset(whole, part)
            if (offset is None or not _box_fits_fragment(candidate.box, preceding.box, offset, len(part), len(whole))
                    or not _box_fits_fragment(candidate.box, following.box, offset, len(part), len(whole))):
                continue
            if candidate.first - end > 3.05:
                break
            supported.append((row, line))
            end = max(end, candidate.last)
        else:
            # 只使用前后都读到全文、且中间有连续识别线索的裁切片段。
            if following.first - end <= 3.05:
                for position in supported:
                    matches[position][whole] = before, after
    clipped = defaultdict(list)
    for (row, line), sources in matches.items():
        if len(sources) == 1:
            before, after = next(iter(sources.values()))
            clipped[row].append([line, before, after])
    return clipped


def evidence_rows(records):
    grouped = defaultdict(list)
    for candidate, text, confidence in records:
        # 同时出现的题卡和名牌可能在不同时间消失。
        grouped[candidate.first, candidate.last].append((candidate, text, confidence))
    rows = []
    groups = sorted(grouped.items())
    for (start, end), lines in groups:
        lines.sort(key=lambda line: (line[0].box[1], line[0].box[0]))
        rows.append({"start_seconds": start, "end_seconds": end,
                     "text": "\n".join(line[1] for line in lines),
                     "confidence": 100 * sum(line[2] for line in lines) / len(lines)})
    for index, sources in _clipped_line_sources([lines for _, lines in groups]).items():
        rows[index]["clipped_lines"] = sources
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
        records, used = read_candidates(engine, crops, candidates, check_stop)
    logger.info("video_ocr samples=%s candidates=%s read=%s retained=%s seconds=%.2f",
                count, len(candidates), used, len(records), monotonic() - start)
    return evidence_rows(records)
