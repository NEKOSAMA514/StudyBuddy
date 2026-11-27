"""密集实操画面按稳定时刻选框，供 Small 读字。"""
from collections import Counter, defaultdict
from itertools import accumulate
from statistics import median


def select_dense_screen_lines(candidates, selected, protected_seconds, check_stop):
    if not selected:
        return selected
    seconds = int(max(c.last for c in candidates) + 1e-6) + 1
    widths = [0.] * ((seconds + 59) // 60)
    for c in selected:
        check_stop()
        left, top, right, bottom = c.box
        widths[int(c.first + 1e-6) // 60] += (right - left) / max(1, bottom - top)

    # 用相邻三分钟的典型文字量排除偶尔翻到一页密集课件的情况。
    busy = [median(widths[max(0, m - 1):m + 2]) >= 400 for m in range(len(widths))]
    if not any(busy):
        return selected

    starts, ends = defaultdict(list), defaultdict(list)
    for c in candidates:
        check_stop()
        left, top, right, bottom = c.box
        if right - left >= 2 * max(1, bottom - top):
            center = (top + bottom) / 2
            starts[int(c.first + 1e-6)].append(center)
            ends[int(c.last + 1e-6)].append(center)

    visible = Counter()
    crowded_seconds = [0] * len(widths)
    turnover = []
    for second in range(seconds):
        check_stop()
        for center in ends.get(second - 1, ()):
            visible[center] -= 1
            if not visible[center]:
                del visible[center]
        visible.update(starts.get(second, ()))
        # 同一行可能有多个文字框。检测坐标中相隔至少 8 像素才另算一行。
        rows, previous = 0, float("-inf")
        for center in sorted(visible):
            if center - previous >= 8:
                rows += 1
                previous = center
        if rows >= 8:
            crowded_seconds[second // 60] += 1
        changes = len(starts.get(second, ())) + len(ends.get(second, ()))
        turnover.append(changes / max(1, rows))

    # 至少四分之一的采样画面同时出现多行文字，才对该分钟降频。
    dense = [high and count * 4 >= min(60, seconds - m * 60)
             for m, (high, count) in enumerate(zip(busy, crowded_seconds))]
    if not any(dense):
        return selected
    sampled = [not dense[t // 60] for t in range(seconds)]
    for start in range(0, seconds, 30):
        if dense[start // 60]:
            window = range(start, min(start + 30, seconds))
            stable = min(window, key=lambda t: (turnover[t], -t))
            sampled[stable] = True

    # 查询包含轨迹的起止秒，因此前缀和的右端取 last + 1。
    sampled_seconds_prefix = [0, *accumulate(sampled)]
    result = []
    for c in selected:
        check_stop()
        first, last = int(c.first + 1e-6), int(c.last + 1e-6)
        if first in protected_seconds or sampled_seconds_prefix[last + 1] > sampled_seconds_prefix[first]:
            result.append(c)
    return result
