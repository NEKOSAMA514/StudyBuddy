"""在同一栏反复刷新多行时，保留操作开始、停顿和结束时的文字。"""
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from statistics import median


@dataclass(frozen=True)
class ColumnRefresh:
    time: float
    left: float
    top: float
    right: float
    bottom: float


def _bands(items, position, width):
    band = []
    for item in sorted(items, key=position):
        if band and position(item) - position(band[0]) > width:
            yield band
            band = []
        band.append(item)
    if band:
        yield band


def _columns(time, candidates):
    lines = [c for c in candidates if c.box[2] - c.box[0] >= c.box[3] - c.box[1]]
    for column in _bands(lines, lambda c: c.box[0], 64):
        if len(column) < 5:
            continue
        height = median(c.box[3] - c.box[1] for c in column)
        rows = list(_bands(column, lambda c: (c.box[1] + c.box[3]) / 2, max(4, .5 * height)))
        start = 0
        for end in range(1, len(rows) + 1):
            if end < len(rows) and rows[end][0].box[1] - rows[end - 1][0].box[3] <= max(24, 2 * height):
                continue
            if end - start >= 5:
                boxes = [c.box for row in rows[start:end] for c in row]
                # 用右边界的八十分位数，避免一行特别长的文字把相邻栏也圈进来。
                right = sorted(box[2] for box in boxes)[len(boxes) * 4 // 5]
                yield ColumnRefresh(time, min(b[0] for b in boxes), min(b[1] for b in boxes),
                                    right, max(b[3] for b in boxes))
            start = end


def _column_sequences(by_start, check_stop):
    sequences, active = [], []
    for time, batch in sorted(by_start.items()):
        check_stop()
        active = [seq for seq in active if time - seq[-1].time <= 10.05]
        for column in _columns(time, batch):
            best_score, match = 0, None
            for seq in active:
                old = seq[-1]
                dx = abs(column.left - old.left)
                overlap = min(column.bottom, old.bottom) - max(column.top, old.top)
                if (old.time == time or dx > 48
                        or overlap < .55 * min(column.bottom - column.top, old.bottom - old.top)):
                    continue
                height = max(column.bottom, old.bottom) - min(column.top, old.top)
                score = overlap / height / (1 + dx / 30)
                if score > best_score:
                    best_score, match = score, seq
            if match is None:
                match = []
                sequences.append(match)
                active.append(match)
            match.append(column)
    return [seq for seq in sequences if len(seq) >= 3]


def _temporary_pages(candidates, check_stop):
    # 用 8 像素网格表示所有可见行的布局。短暂切到另一页再返回时，保留中间页。
    starts, ends = defaultdict(list), defaultdict(list)
    stride = 1 + max((ceil(c.box[2] / 8) for c in candidates), default=0)
    for c in candidates:
        check_stop()
        left, top, right, bottom = c.box
        x, y = max(0, int(left // 8)), max(0, int(top // 8))
        width, height = max(1, ceil(right / 8) - x), max(1, ceil(bottom / 8) - y)
        stripe = ((1 << width) - 1) << x
        mask = sum(stripe << (stride * row) for row in range(y, y + height))
        starts[int(c.first + 1e-6)].append((c.offset, mask))
        ends[int(c.last + 1e-6) + 1].append(c.offset)

    layouts, visible = [], {}
    for second in range(max(ends, default=0)):
        check_stop()
        for offset in ends.get(second, ()):
            visible.pop(offset, None)
        visible.update(starts.get(second, ()))
        layout = 0
        for mask in visible.values():
            layout |= mask
        layouts.append(layout)
    protect = set()
    for second, entries in starts.items():
        if second == 0 or len(entries) < 5:
            continue
        old, current = layouts[second - 1], layouts[second]
        change = (old ^ current).bit_count()
        if change < max(35, .16 * (old | current).bit_count()):
            continue
        for later in layouts[second + 1:second + 3]:
            if (old ^ later).bit_count() < .5 * min(change, (current ^ later).bit_count()):
                protect.add(second)
                break
    return protect


def _isolated_labels(by_start, keep, check_stop):
    # 先补回没有同行邻框的短标签。位于连续刷新栏内的临时文字仍由后续步骤筛选。
    visible = []
    for time, batch in sorted(by_start.items()):
        visible = [c for c in visible if c.last >= time]
        visible.extend(batch)
        for c in batch:
            check_stop()
            if c.offset in keep:
                continue
            left, top, right, bottom = c.box
            height = bottom - top
            if right - left > 4 * height:
                continue
            for other in visible:
                if other is c:
                    continue
                x, y, end, base = other.box
                if (abs(top + bottom - y - base) <= min(height, base - y)
                        and max(x - right, left - end) <= 3 * height):
                    break
            else:
                yield c.offset


def select_burst_versions(candidates, selected, check_stop):
    by_start, by_second = defaultdict(list), defaultdict(list)
    for c in candidates:
        by_start[c.first].append(c)
        by_second[int(c.first + 1e-6)].append(c)
    sequences = _column_sequences(by_start, check_stop)
    protected = _temporary_pages(candidates, check_stop)
    keep = {c.offset for c in selected}
    keep.update(_isolated_labels(by_start, keep, check_stop))
    for seq in sequences:
        anchors = [seq[0].time]
        for current, following in zip(seq[1:-1], seq[2:]):
            if following.time - current.time > 4.05 or current.time - anchors[-1] >= 20:
                anchors.append(current.time)
        anchors.append(seq[-1].time)
        for current, following in zip(seq, seq[1:]):
            check_stop()
            if following.time - current.time > 4.05:
                continue
            for second in range(int(current.time), int(following.time) + 1):
                if second in protected:
                    continue
                for c in by_second.get(second, ()):
                    if not current.time <= c.first < following.time or c.last - c.first >= 4:
                        continue
                    if anchors[bisect_left(anchors, c.first)] <= c.last:
                        continue
                    x, y, right, bottom = c.box
                    if (current.left - 8 <= (x + right) / 2 <= current.right + 8
                            and current.top - 4 <= (y + bottom) / 2 <= current.bottom + 4):
                        keep.discard(c.offset)
    for second in protected:
        keep.update(c.offset for c in by_second[second])
    return [c for c in candidates if c.offset in keep], protected
