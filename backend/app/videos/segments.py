"""按原文归属分段；负责范围前一分钟的证据单独作为上下文。"""
from bisect import bisect_left, bisect_right
from itertools import groupby
from ..analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT
from ..analysis.content_types import CONTENT_TYPES

SEGMENT_SECONDS = 1200
CONTEXT_SECONDS = 60
FLOW_VERSION = "video-flow-v13-raw-outline"


def outline_overhead(model, video_title=""):
    payload = {"evidence": [], "context": []}
    if video_title:
        payload["video_title"] = video_title
    return model.input_characters("outline", payload)


# 只取章节开始前的有限时间窗口，作为前文而不改变主要证据归属。
def preceding_context(evidence, start):
    """按证据时间选取前一分钟；跨窗口起点的语音整条保留，不猜测文字时间。"""
    left = max(0, start - CONTEXT_SECONDS)
    return [item for item in evidence if item["start_seconds"] < start and
            (item["start_seconds"] >= left or item["end_seconds"] > left)]


# 同时按视频时间和原始材料字数分段，同一时间点的证据整组保留。
def split_segments(evidence, duration, limit=None, overhead=0):
    limit = DEFAULT_INPUT_TEXT_LIMIT if limit is None else limit
    starts, offsets, prefix_characters = [], [0], [0]
    for seconds, records in groupby(evidence, key=lambda item: item["start_seconds"]):
        group = list(records)
        starts.append(seconds)
        offsets.append(offsets[-1] + len(group))
        prefix_characters.append(prefix_characters[-1] + sum(len(item["text"]) for item in group))

    available = limit - overhead
    segmented = duration > SEGMENT_SECONDS or prefix_characters[-1] > available
    if not segmented:
        return False, [{"index": 1, "start_seconds": 0.0, "end_seconds": duration, "primary": evidence}]

    # 续段会收到首段选定的类型说明，分段时按最长可选说明计量。
    continuation = max(map(len, CONTENT_TYPES.values()))
    segments, first_group, start = [], 0, 0.0
    while first_group < len(starts):
        room = available
        if first_group:
            room -= continuation + sum(len(item["text"]) for item in preceding_context(evidence, start))
        time_limit = bisect_left(starts, start + SEGMENT_SECONDS, lo=first_group + 1)
        # 至少保留第一组，超长单组沿用原有的段内提取流程。
        character_limit = max(prefix_characters[first_group] + room, prefix_characters[first_group + 1])
        next_group = bisect_right(prefix_characters, character_limit, lo=first_group + 1, hi=time_limit + 1) - 1
        end = starts[next_group] if next_group < len(starts) else duration
        segments.append({"index": len(segments) + 1, "start_seconds": start,
                         "end_seconds": end, "primary": evidence[offsets[first_group]:offsets[next_group]]})
        first_group, start = next_group, end
    return True, segments
