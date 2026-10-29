"""公开响应字段；数据库内部设置和结果不随任务轮询发送。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PublicRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class VideoPublic(PublicRecord):
    id: str
    filename: str
    size_bytes: int
    duration: float
    video_codec: str
    audio_codec: str | None
    media_info: dict | None
    pinned: bool
    created_at: str


class EvidencePublic(PublicRecord):
    id: str
    video_id: str
    kind: str
    start_seconds: float
    end_seconds: float
    text: str
    image_name: str | None
    confidence: float | None


# 显式限定轮询可见字段，内部任务配置、凭据和正文不随数据库对象暴露。
class TaskPublic(PublicRecord):
    activity: dict | None = None
    id: str
    video_id: str
    status: str
    stage: str
    attempts: int
    config: dict
    created_at: str
    updated_at: str
    error: dict | None
    warnings: list
    timings: dict
    progress: dict
    notes_state: Literal["none", "partial", "ready"]
    notes_revision: int
    segment_progress: dict | None
    note_style: Literal["concise", "detailed"] = "detailed"


class NotesPublic(PublicRecord):
    id: str
    video_id: str
    format_version: Literal[4]
    title: str
    tags: list[str]
    overview_paragraphs: list[str]
    chapters: list[dict]
    source_issues: list[dict]
    evidence_ids: list[str]
    complete: bool
    covered_until_seconds: float
    model: str
    prompt_version: str
    coverage_note: str
    basis: list[str]
    warnings: list
    timings: dict
    recognition: dict | None = None
    usage: dict = Field(default_factory=dict)
    model_requests: int = 0
    notes_revision: int = 0
    completed_segments: int | None = None
    total_segments: int | None = None
    segment_guides: list[dict] = Field(default_factory=list)
    generation_key: str | None = None
    note_style: Literal["concise", "detailed"] = "detailed"
    content_type: str | None = None
    source_cursor: dict | None = None
