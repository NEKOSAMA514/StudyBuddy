"""挑选送给 Small 的文字框，保留同一位置连续显示的首尾版本。"""
from collections import defaultdict
from heapq import heappop, heappush
import zlib

import cv2
import numpy as np


def crop_image(crops, candidate):
    crops.seek(candidate.offset)
    pixels = zlib.decompress(crops.read(candidate.length))
    return np.frombuffer(pixels, dtype=np.uint8).reshape(candidate.shape)


def _position_runs(candidates, check_stop):
    ordered = sorted(candidates, key=lambda c: (c.first, c.box[1], c.box[0], c.last, c.offset))
    rows = defaultdict(list)
    runs = []
    for candidate in ordered:
        check_stop()
        left, top, right, bottom = candidate.box
        center = (top + bottom) / 2
        row = int(center // 8)
        best = None
        for nearby in (row - 1, row, row + 1):
            rows[nearby] = [index for index in rows[nearby] if runs[index][-1].last >= candidate.first - 6.05]
            for index in rows[nearby]:
                previous = runs[index][-1]
                if previous.last >= candidate.first:
                    continue
                old_left, old_top, old_right, old_bottom = previous.box
                distance = abs((old_top + old_bottom) / 2 - center)
                if distance > max(2, min(4, .35 * min(bottom - top, old_bottom - old_top))):
                    continue
                shared = min(old_right, right) - max(old_left, left)
                width = max(old_right, right) - min(old_left, left)
                if shared >= .6 * width:
                    score = (shared / width, -distance, previous.last, index, nearby)
                    if best is None or score > best:
                        best = score
        if best is None:
            index = len(runs)
            runs.append([])
        else:
            index, previous_row = best[-2:]
            rows[previous_row].remove(index)
        runs[index].append(candidate)
        rows[row].append(index)
    return runs


def _removal_cost(candidate, neighbor, signatures):
    difference = cv2.absdiff(signatures[candidate.offset], signatures[neighbor.offset])
    # 比较局部字形，避免一整行的平均值掩盖单个数字的变化。
    glyph_change = difference.reshape(4, 4, 12, 8).mean(axis=(1, 3)).max()
    gap = max(0, neighbor.first - candidate.last, candidate.first - neighbor.last)
    return float(glyph_change) + 2 * gap


def select_for_reading(crops, candidates, check_stop):
    to_skip = len(candidates) - round(len(candidates) * .75)
    if not to_skip or len(candidates) < 3:
        return candidates

    by_offset = {}
    signatures = {}
    neighbors = {}
    queue = []

    def offer(offset):
        before, after = neighbors[offset]
        if before is None or after is None:
            return
        candidate = by_offset[offset]
        cost = min(_removal_cost(candidate, by_offset[other], signatures) for other in (before, after))
        heappush(queue, (cost + 2 * min(8, candidate.samples), offset, before, after))

    for run in _position_runs(candidates, check_stop):
        if len(run) < 3:
            continue
        for index, candidate in enumerate(run):
            check_stop()
            offset = candidate.offset
            by_offset[offset] = candidate
            small = cv2.resize(crop_image(crops, candidate), (96, 16), interpolation=cv2.INTER_AREA)
            signatures[offset] = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            before = run[index - 1].offset if index else None
            after = run[index + 1].offset if index + 1 < len(run) else None
            neighbors[offset] = before, after
        for candidate in run[1:-1]:
            offer(candidate.offset)

    skipped = set()
    while queue and len(skipped) < to_skip:
        check_stop()
        _, offset, before, after = heappop(queue)
        if neighbors.get(offset) != (before, after):
            continue
        del neighbors[offset]
        skipped.add(offset)
        neighbors[before] = neighbors[before][0], after
        neighbors[after] = before, neighbors[after][1]
        offer(before)
        offer(after)
    return [candidate for candidate in candidates if candidate.offset not in skipped]
