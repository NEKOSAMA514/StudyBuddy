"""文档材料的稳定来源、字符分段和输入预算装包；不调用模型。"""
import hashlib
import json
import re
from ..errors import AppError
from ..analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT

MAX_DOCUMENT_CHARACTERS = 100_000
MAX_DOCUMENT_SEGMENTS = 8
MAX_DOCUMENT_REQUESTS = 80
CONTEXT_CHARACTERS = 1000
PREVIOUS_OUTLINE_CHARACTERS = 1000


# 文档身份使用紧凑排序 JSON 的完整 SHA-256，不能与视频短哈希混用。
def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# 缩减上一章大纲的发送副本以满足上下文预算，不改写已保存大纲。
def compact_outline(outline):
    if not outline:
        return {}
    value = {"title": outline["title"], "core_question": outline["core_question"], "topics": list(outline["topics"])}
    def size():
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    while value["topics"] and size() > PREVIOUS_OUTLINE_CHARACTERS:
        value["topics"].pop()
    for field in ("core_question", "title"):
        if size() <= PREVIOUS_OUTLINE_CHARACTERS:
            break
        text = value[field]
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            value[field] = text[:middle]
            if size() <= PREVIOUS_OUTLINE_CHARACTERS:
                low = middle
            else:
                high = middle - 1
        value[field] = text[:low]
    return value


# 校验提取内容的版本、来源编号和字段结构，避免损坏材料进入生成流程。
def load_material(folder):
    try:
        value = json.loads((folder / "content.json").read_text(encoding="utf-8"))
        units = value["units"]
        if not isinstance(value["version"], int) or not isinstance(units, list):
            raise ValueError("invalid document material")
        ids = set()
        for unit in units:
            if (not re.fullmatch(r"D[1-9]\d*", unit["id"]) or unit["id"] in ids or
                    not isinstance(unit["text"], str) or not isinstance(unit["locator"], dict) or
                    not isinstance(unit["label"], str) or unit["format"] not in {"text", "markdown"}):
                raise ValueError("invalid document source")
            ids.add(unit["id"])
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AppError("document_content_missing", "提取内容无法读取，请重新上传原文件。", 409) from exc


# 记录材料内容与来源位置的稳定摘要，供后续恢复确认输入未被替换。
def snapshot(material):
    return {"extraction_version": material["version"], "units_digest": digest(material["units"]),
            "unit_count": len(material["units"]), "character_count": sum(len(u["text"]) for u in material["units"])}


def preceding(fragments, index):
    result, remaining = [], CONTEXT_CHARACTERS
    for item in reversed(fragments[:index]):
        if not remaining:
            break
        text = item["text"][-remaining:]
        result.append({**item, "text": text, "start_offset": item["end_offset"] - len(text)})
        remaining -= len(text)
    return list(reversed(result))


# 按字符预算切片并保留来源偏移，优先在换行处分割并预留前文空间。
def split_material(material, limit=DEFAULT_INPUT_TEXT_LIMIT):
    fragments = []
    # 单个来源超过一次请求时，也为前文上下文预留空间。
    size = max(1, limit - min(2 * CONTEXT_CHARACTERS, limit // 2))
    for unit in material["units"]:
        if not unit["text"].strip():
            continue
        text, start = unit["text"], 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                newline = text.rfind("\n", start + size // 2, end)
                if newline >= 0:
                    end = newline + 1
            fragments.append({"id": f"F{len(fragments) + 1}", "unit_id": unit["id"], "label": unit["label"],
                              "start_offset": start, "end_offset": end, "text": text[start:end]})
            start = end
    segments, group, length = [], [], 0
    for index, fragment in enumerate(fragments):
        ceiling = limit if not segments else max(1, limit - min(CONTEXT_CHARACTERS, limit // 4))
        if group and length + len(fragment["text"]) > ceiling:
            segments.append(group)
            group, length = [], 0
        if not group:
            length = sum(len(c["text"]) for c in preceding(fragments, index))
        group.append(fragment)
        length += len(fragment["text"])
    if group:
        segments.append(group)
    return fragments, segments


# 反复用实际请求计量校准分组，确保正文、前文和大纲一起满足预算。
def detailed_segments(material, model, title):
    from ..analysis.content_types import CONTENT_TYPES
    # 为实际请求中的最长类型说明和受限上一章大纲预留预算。
    build = lambda primary, context: {"document_title": title, "anchors": [i["id"] for i in primary],
        "evidence": [{"id": i["id"], "text": i["text"]} for i in primary], "context": context,
        "previous_chapter_outline": {}, "content_type": max(CONTENT_TYPES.values(), key=len)}
    fixed = model.input_characters("outline", build([], []))
    reserve = PREVIOUS_OUTLINE_CHARACTERS
    available = model.settings.llm_input_text_limit - fixed - reserve
    if available <= 0:
        raise AppError("model_input_budget", "提示词未留下原文空间，请提高生成输入上限。")
    # 材料切片保持稳定编号和偏移，预算只计入发送的输入文字。
    while True:
        fragments, groups = split_material(material, available)
        sizes = [reserve + model.input_characters("outline", build(group, [{"id": c["id"], "text": c["text"]}
                 for c in preceding(fragments, fragments.index(group[0]))])) for group in groups]
        excess = max(sizes, default=0) - model.settings.llm_input_text_limit
        if excess <= 0:
            return fragments, groups
        available -= max(1, excess)
        if available <= 0:
            raise AppError("model_input_budget", "上下文未留下原文空间，请提高输入上限。")


def verify_snapshot(material, expected):
    if snapshot(material) != expected:
        raise AppError("document_source_changed", "本任务的提取材料已改变，请重新生成笔记。", 409)
