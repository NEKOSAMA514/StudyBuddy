"""按原文与大纲请求预算分段；正文另取章前一分钟的原始证据。"""
from bisect import bisect_left, bisect_right
from itertools import groupby
from ..analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT
from ..analysis.content_types import CONTENT_TYPES
from .model_input import evidence_request_text

SEGMENT_SECONDS = 1200
CONTEXT_SECONDS = 60
FLOW_VERSION = "video-flow-v18-outline-sources-context"


def outline_overhead(model, video_title="", context=None, content_type=None):
    payload = {"evidence": [], "context": context or {}}
    if video_title:
        payload["video_title"] = video_title
    if content_type:
        payload["content_type"] = content_type
    return model.input_characters("outline", payload) + model.repair_input_reserve


# 只取章节开始前的有限时间窗口，作为前文而不改变主要证据归属。
def preceding_context(evidence, start):
    """按证据时间选取前一分钟；跨窗口起点的语音整条保留，不猜测文字时间。"""
    left = max(0, start - CONTEXT_SECONDS)
    return [item for item in evidence if item["start_seconds"] < start and
            (item["start_seconds"] >= left or item["end_seconds"] > left)]


class SegmentPlan:
    def __init__(self, evidence, duration):
        self.duration = duration
        self.starts, self.offsets, self.characters = [], [0], [0]
        for seconds, records in groupby(evidence, key=lambda item: item["start_seconds"]):
            group = list(records)
            self.starts.append(seconds)
            self.offsets.append(self.offsets[-1] + len(group))
            self.characters.append(self.characters[-1] + sum(len(evidence_request_text(item)) for item in group))

    def ranges(self, start, room, later_room=None):
        """返回时间范围与证据下标，下一段按实际容量切分，后续段数按可用信息估计。"""
        starts, offsets, characters = self.starts, self.offsets, self.characters
        first_group = bisect_left(starts, start)
        if self.duration - start <= SEGMENT_SECONDS and characters[-1] - characters[first_group] <= room:
            return False, [(start, self.duration, offsets[first_group], offsets[-1])]

        if later_room is None:
            # 预估时还没有已选的内容类型或已生成的大纲。
            later_room = room - max(map(len, CONTENT_TYPES.values()))
        ranges = []
        while first_group < len(starts):
            time_limit = bisect_left(starts, start + SEGMENT_SECONDS, lo=first_group + 1)
            # 同一时刻的证据整组保留，超长单组沿用段内提取流程。
            character_limit = max(characters[first_group] + room, characters[first_group + 1])
            next_group = bisect_right(characters, character_limit, lo=first_group + 1, hi=time_limit + 1) - 1
            end = starts[next_group] if next_group < len(starts) else self.duration
            ranges.append((start, end, offsets[first_group], offsets[next_group]))
            first_group, start, room = next_group, end, later_room
        return True, ranges


def split_segments(evidence, duration, limit=None, overhead=0):
    limit = DEFAULT_INPUT_TEXT_LIMIT if limit is None else limit
    segmented, ranges = SegmentPlan(evidence, duration).ranges(0.0, limit - overhead)
    return segmented, [{"index": index, "start_seconds": start, "end_seconds": end, "primary": evidence[left:right]}
                       for index, (start, end, left, right) in enumerate(ranges, 1)]
