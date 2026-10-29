"""共用模型输出契约，不包含特定来源的位置字段。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .content_types import CONTENT_TYPES, complete_content_type


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConciseNote(Record):
    content_type: str = Field(json_schema_extra={"enum": list(CONTENT_TYPES)})
    title: str = Field(min_length=1)
    markdown: str = Field(min_length=1)

    @field_validator("content_type")
    @classmethod
    def normalize_type(cls, value):
        return complete_content_type(value)


class ChapterPlan(Record):
    title: str = Field(min_length=1)
    core_question: str = Field(min_length=1)
    topics: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    start_anchor_id: str = Field(min_length=1)


class Fact(Record):
    text: str = Field(min_length=1)


class SourceIssue(Record):
    kind: Literal["source_issue"]
    quote: str | None = None
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    critical: bool = True


class Extracted(Record):
    facts: list[Fact]
    issues: list[dict] = Field(default_factory=list)


class ChapterDraft(Record):
    chapter_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    markdown: str = Field(min_length=1)
    issues: list[dict] = Field(default_factory=list)


class ChapterBatch(Record):
    chapters: list[ChapterDraft] = Field(min_length=1)


class QuoteRepair(Record):
    chapter_id: str = Field(min_length=1)
    block_index: int = Field(ge=0)
    quote: str = Field(min_length=1)


class QuoteRepairs(Record):
    repairs: list[QuoteRepair] = Field(min_length=1)


class Overview(Record):
    title: str = Field(min_length=1)
    tags: list[Annotated[str, Field(min_length=1)]]
    overview_paragraphs: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
