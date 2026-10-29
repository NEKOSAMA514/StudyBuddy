"""文档模型输出、请求及安全任务摘要的结构契约。"""
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ..analysis.schemas import Record, ChapterPlan, Extracted, ChapterBatch, Overview, QuoteRepairs
from .material import MAX_DOCUMENT_REQUESTS
from ..analysis.content_types import CONTENT_TYPES, complete_content_type
from ..analysis.schemas import ConciseNote
from typing import Literal

class DocumentOutline(Record):
    chapters: list[ChapterPlan] = Field(min_length=1)
    document_summary: str = Field(min_length=1)
    content_type: str = Field(json_schema_extra={"enum": list(CONTENT_TYPES.values())})

    @field_validator("content_type")
    @classmethod
    def complete_content_type(cls, value):
        return complete_content_type(value)

DOCUMENT_SCHEMAS = {"outline": DocumentOutline, "extract": Extracted, "chapters": ChapterBatch, "overview": Overview, "issue_quotes": QuoteRepairs, "concise": ConciseNote}

class DocumentAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    note_style: Literal["concise", "detailed"] | None = None


class DocumentExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ocr_option: Literal["rapidocr"] | None = None
    parser: Literal["local", "mineru_agent", "mineru_vlm"] | None = None
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    note_style: Literal["concise", "detailed"] | None = None

STAGES = {"checking": "检查文档材料", "planning": "规划文档章节", "summarizing": "生成章节正文", "finalizing": "整理概览与校验"}

def public_task(task):
    from ..analysis.activity import public_activity
    result = task.result or {}
    style = task.config.get("note_style", "detailed")
    return {**{k: getattr(task, k) for k in ("id", "document_id", "status", "stage", "attempts", "created_at", "updated_at",
                                          "error", "warnings", "timings", "notes_revision", "request_count", "completed_at")},
            "note_style": style, "activity": public_activity(task),
            "config": {k: task.config[k] for k in ("source", "outline_preview", "total_segments") if k in task.config},
            "notes_state": ("ready" if result.get("complete") else "partial") if result.get("chapters") else "none",
            "progress": {"stage_labels": {"checking": "检查文档材料", "concise": "生成简洁笔记"} if style == "concise" else STAGES, "segment": task.config.get("segment", 0),
                         "total_segments": task.config.get("total_segments", 0)},
            "request_limit": MAX_DOCUMENT_REQUESTS}


def public_document(row):
    fields = ("id", "filename", "format", "size_bytes", "unit_count", "character_count", "warnings", "pinned", "created_at")
    return {"kind": "document", **{key: getattr(row, key) for key in fields},
            "extracted": not row.fingerprint.startswith("pending:"), "latest_task": None, "extraction": None}
