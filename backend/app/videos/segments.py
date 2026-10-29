"""按原文归属分段；负责范围前一分钟的证据单独作为上下文。"""
from itertools import groupby
from ..analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT
from .model_input import outline_text_length

SEGMENT_SECONDS = 1200
CONTEXT_SECONDS = 60
FLOW_VERSION = "video-flow-v9-content-budget"


def outline_measure(model, video_title=""):
    def measure(payload):
        return model.input_characters("outline", {**payload,
            **({"video_title": video_title} if video_title else {})})
    return measure


# 只取章节开始前的有限时间窗口，作为前文而不改变主要证据归属。
def preceding_context(evidence, start):
    """按证据时间选取前一分钟；跨窗口起点的语音整条保留，不猜测文字时间。"""
    left = max(0, start - CONTEXT_SECONDS)
    return [item for item in evidence if item["start_seconds"] < start and
            (item["start_seconds"] >= left or item["end_seconds"] > left)]


# 同时按视频时间和实际输入文字划分连续片段，不截掉超预算材料。
def split_segments(evidence, duration, limit=None, measure=outline_text_length):
    limit = DEFAULT_INPUT_TEXT_LIMIT if limit is None else limit
    segmented = duration > SEGMENT_SECONDS or measure({"evidence": evidence, "context": []}) > limit
    if not segmented:
        return False, [{"index": 1, "start_seconds": 0.0, "end_seconds": duration, "primary": evidence}]
    groups, current, start = [], [], 0.0
    for seconds, records in groupby(evidence, key=lambda item: item["start_seconds"]):
        records = list(records)
        context = preceding_context(evidence, start)
        count = measure({"evidence": [*current, *records], "context": context})
        if current and (seconds >= start + SEGMENT_SECONDS or count > limit and
                        count > measure({"evidence": current, "context": context})):
            groups.append({"index": len(groups) + 1, "start_seconds": start,
                           "end_seconds": seconds, "primary": current})
            current, start = [], seconds
        current.extend(records)
    if current:
        groups.append({"index": len(groups) + 1, "start_seconds": start,
                       "end_seconds": duration, "primary": current})
    return True, groups
