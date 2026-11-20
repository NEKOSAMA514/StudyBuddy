"""按字符范围装包并检查覆盖，不依赖领域服务或生成循环。"""
from bisect import bisect_right
from itertools import accumulate
from .content_types import CONTENT_TYPES
from ..errors import AppError


def fragment(source, start, end):
    return {**source, "text": source["text"][start:end], "start_offset": start, "end_offset": end}


def trailing_context(items, characters):
    kept = []
    for item in reversed(items):
        if characters <= 0:
            break
        size = min(characters, len(item["text"]))
        kept.append({**item, "text": item["text"][-size:], "start_offset": item["end_offset"] - size})
        characters -= size
    return list(reversed(kept))


# 按实际请求预算切分来源字符，保留必要前文并保证正文范围连续覆盖。
def plan_segments(sources, build, measure, limit, context_for):
    sources = [s for s in sources if s["text"].strip()]
    # 类型只改变提示词、Schema 和类型字段；来源文字在各类型中的计量相同。
    empty = build([], [])
    overheads = {None: measure(empty)}
    for content_type in CONTENT_TYPES.values():
        overheads[content_type] = measure({**empty, "content_type": content_type})
    largest_type = max(overheads, key=overheads.get)
    fixed = overheads[largest_type]

    def request_size(primary, context):
        payload = build(primary, context)
        if largest_type:
            payload["content_type"] = largest_type
        return measure(payload)

    if fixed >= limit:
        raise AppError("model_input_budget", "提示词与补充要求未留下材料空间，请提高生成输入上限。")
    characters = list(accumulate((len(s["text"]) for s in sources), initial=0))
    segments, processed, index, offset = [], [], 0, 0
    while index < len(sources):
        context = context_for(processed, sources[index])
        ceiling = fixed + (limit - fixed) // 2
        high = sum(len(s["text"]) for s in context)
        low = high if high <= ceiling - fixed else 0
        while low < high:
            middle = (low + high + 1) // 2
            if request_size([], trailing_context(context, middle)) <= ceiling:
                low = middle
            else:
                high = middle - 1
        context = trailing_context(context, low)
        primary = []
        # 原文长度是去重后材料的上界。先整批装入必定能放下的前缀，再按真实请求逐条试探余量。
        room = limit - fixed - low
        end = bisect_right(characters, characters[index] + offset + room, lo=index + 1) - 1
        if end > index:
            primary = [fragment(s, offset if i == index else 0, len(s["text"]))
                       for i, s in enumerate(sources[index:end], index)]
            if request_size(primary, context) <= limit:
                index, offset = end, 0
            else:
                primary = []
        while index < len(sources):
            source = sources[index]
            remaining = fragment(source, offset, len(source["text"]))
            if request_size([*primary, remaining], context) <= limit:
                primary.append(remaining)
                index, offset = index + 1, 0
                continue
            if primary:
                break
            low, high = offset, len(source["text"])
            while low < high:
                middle = (low + high + 1) // 2
                if request_size([fragment(source, offset, middle)], context) <= limit:
                    low = middle
                else:
                    high = middle - 1
            if low == offset:
                raise AppError("model_input_budget", "来源编号及上下文未留下材料空间，请提高生成输入上限。")
            end = low
            if end < len(source["text"]):
                for separator in ("\n", "。", "！", "？", ";", ". "):
                    boundary = source["text"].rfind(separator, offset + (end - offset) // 2, end)
                    if boundary >= 0:
                        preferred = boundary + len(separator)
                        # 截短后可能改变前文中重复画面的去重结果，复核最终字符范围。
                        if request_size([fragment(source, offset, preferred)], context) <= limit:
                            end = preferred
                        break
            primary.append(fragment(source, offset, end))
            offset = end
            if offset == len(source["text"]):
                index, offset = index + 1, 0
            break
        segments.append({"primary": primary, "context": context})
        processed.extend(primary)
    if not segments:
        raise AppError("no_evidence", "没有可用于生成笔记的文字。")
    validate_coverage(sources, segments, complete=True)
    return segments


def validate_coverage(sources, segments, complete=False):
    sources = [s for s in sources if s["text"].strip()]
    index, offset = 0, 0
    for segment in segments:
        for item in segment["primary"]:
            if index >= len(sources):
                raise AppError("invalid_checkpoint", "来源覆盖包含多余材料。")
            source = sources[index]
            if (item["id"] != source["id"] or item["start_offset"] != offset or
                    not offset < item["end_offset"] <= len(source["text"]) or
                    item != fragment(source, offset, item["end_offset"])):
                raise AppError("invalid_checkpoint", "来源字符范围不连续或与原文不符。")
            offset = item["end_offset"]
            if offset == len(source["text"]):
                index, offset = index + 1, 0
    if complete and index != len(sources):
        raise AppError("invalid_checkpoint", "来源覆盖遗漏了材料。")
    return index, offset


def validate_contexts(sources, segments):
    originals, processed = {s["id"]: s for s in sources}, {}
    for segment in segments:
        for item in segment["context"]:
            source = originals.get(item["id"])
            if (not source or not 0 <= item["start_offset"] < item["end_offset"] <= processed.get(item["id"], 0) or
                    item != fragment(source, item["start_offset"], item["end_offset"])):
                raise AppError("invalid_checkpoint", "上下文不是已处理的真实原文。")
        for item in segment["primary"]:
            processed[item["id"]] = item["end_offset"]
