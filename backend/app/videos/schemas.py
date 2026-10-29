"""视频大纲、结果版本与时间戳依据校验。"""
import math
from pydantic import Field, field_validator
from ..errors import AppError
from ..analysis.schemas import Record, ChapterPlan, Extracted, ChapterBatch, Overview, QuoteRepairs
from ..analysis.content_types import CONTENT_TYPES, complete_content_type
from ..analysis.schemas import ConciseNote

FORMAT_VERSION = 4


class Outline(Record):
    chapters: list[ChapterPlan] = Field(min_length=1)
    video_summary: str = Field(min_length=1)
    content_type: str = Field(json_schema_extra={"enum": list(CONTENT_TYPES.values())})

    @field_validator("content_type")
    @classmethod
    def complete_content_type(cls, value):
        return complete_content_type(value)

SCHEMAS = {"outline": Outline, "extract": Extracted, "chapters": ChapterBatch, "overview": Overview, "issue_quotes": QuoteRepairs, "concise": ConciseNote}


def ground_timestamps(ids, evidence, duration):
    if not ids or any(i not in evidence for i in ids):
        raise AppError("invalid_evidence", "笔记引用了不存在的证据。")
    for identifier in ids:
        item = evidence[identifier]
        if not (math.isfinite(item["start_seconds"]) and math.isfinite(item["end_seconds"])
                and 0 <= item["start_seconds"] <= item["end_seconds"] <= duration):
            raise AppError("invalid_timestamp", "原始证据时间范围无效。")
    basis = "speech" if any(evidence[i]["kind"] == "speech" for i in ids) else "frame"
    anchors = [evidence[i] for i in ids if evidence[i]["kind"] == basis]
    return {"start_seconds": min((item["start_seconds"] for item in anchors), default=None),
            "end_seconds": max((item["end_seconds"] for item in anchors), default=None),
            "timestamp_basis": basis if anchors else None}


def current_notes(result):
    return bool(result and result.get("format_version") == FORMAT_VERSION and result.get("chapters"))


def ground_chapters(chapters, issues, evidence, duration, *, start=0.0, end=None):
    end = duration if end is None else end
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration):
        raise AppError("invalid_timestamp", "笔记覆盖范围无效。")
    result, seen = [], set()
    previous = start
    for chapter in chapters:
        if chapter["chapter_id"] in seen:
            raise AppError("invalid_evidence", "笔记章节编号重复。")
        seen.add(chapter["chapter_id"])
        location = ground_timestamps(chapter["evidence_ids"], evidence, duration)
        left, right = chapter["start_seconds"], chapter["end_seconds"]
        if not (math.isfinite(left) and math.isfinite(right) and left == previous < right <= end):
            raise AppError("invalid_timestamp", "章节时间范围不连续或无效。")
        previous = right
        result.append({**chapter, "timestamp_basis": location["timestamp_basis"]})
    if previous != end:
        raise AppError("invalid_timestamp", "章节没有覆盖已完成范围的末尾。")
    for issue in issues:
        ground_timestamps(issue["evidence_ids"], evidence, duration)
        if issue["chapter_id"] not in seen or issue.get("quote") and not any(
                issue["quote"] in evidence[i]["text"] for i in issue["evidence_ids"]):
            raise AppError("invalid_evidence", "存疑原文与对应证据不一致。")
    return result
