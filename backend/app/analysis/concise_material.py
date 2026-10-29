"""按字符范围装包并检查覆盖，不依赖领域服务或生成循环。"""
from .content_types import CONTENT_TYPES
from ..errors import AppError


def fragment(source, start, end):
    return {**source, "text": source["text"][start:end], "start_offset": start, "end_offset": end}


def measure_largest(build, measure, primary, context):
    payload = build(primary, context)
    return max(measure(payload), *(measure({**payload, "content_type": value}) for value in CONTENT_TYPES.values()))


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
    fixed = measure_largest(build, measure, [], [])
    if fixed >= limit:
        raise AppError("model_input_budget", "提示词与补充要求未留下材料空间，请提高生成输入上限。")
    segments, processed, index, offset = [], [], 0, 0
    while index < len(sources):
        context = context_for(processed, sources[index])
        ceiling = fixed + (limit - fixed) // 2
        low, high = 0, sum(len(s["text"]) for s in context)
        while low < high:
            middle = (low + high + 1) // 2
            if measure_largest(build, measure, [], trailing_context(context, middle)) <= ceiling:
                low = middle
            else:
                high = middle - 1
        context = trailing_context(context, low)
        primary = []
        while index < len(sources):
            source = sources[index]
            remaining = fragment(source, offset, len(source["text"]))
            if measure_largest(build, measure, [*primary, remaining], context) <= limit:
                primary.append(remaining)
                index, offset = index + 1, 0
                continue
            if primary:
                break
            low, high = offset, len(source["text"])
            while low < high:
                middle = (low + high + 1) // 2
                if measure_largest(build, measure, [fragment(source, offset, middle)], context) <= limit:
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
                        end = boundary + len(separator)
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
