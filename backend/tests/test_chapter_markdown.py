from app.analysis.content_types import CONTENT_TYPES
import asyncio
import json
import pytest
from app.analysis.markdown import parse_chapters
from app.videos.detailed import DetailedVideoGeneration
from app.videos.model import ModelClient
from app.analysis.markdown import validate_markdown
from app.config import Settings
from app.errors import AppError
from .fakes import model_content


def test_direct_markdown_keeps_rich_body_and_literal_protocol_in_code():
    body = "#### 说明\n\n正文**加粗**、`E1`与反斜杠 \\path。\n\n|对象|数值|\n|---|---|\n|甲|1|\n\n- 条目\n  - 子条目\n\n> 引用\n\n```html\n<!-- chapter:C999 -->\n<!-- evidence:FAKE -->\n```"
    draft = {"chapter_id": "C1", "summary": "摘要", "markdown": body, "issues": []}
    assert parse_chapters(model_content({"chapters": [draft]})) == {"chapters": [draft]}
    validate_markdown(body)


@pytest.mark.parametrize("body", ["E1电路的工作状态。", "`[E1]`", "```\n<!-- evidence:E1 -->\n```",
    "- 条目\n" * 30, "正文。\n\n" * 100, "|甲|乙|\n|--|--|\n|1|2|"])
def test_free_writing_is_not_restricted_by_shape(body):
    validate_markdown(body)


@pytest.mark.parametrize("body", ["", "#### 只有标题", "<!-- evidence:E1 -->\n正文", "正文[E1]",
    "正文 <!-- issue:{} -->", "依据证据E12整理。"])
def test_empty_body_and_leaked_protocol_require_repair(body):
    with pytest.raises(ValueError):
        validate_markdown(body)


def test_issue_marker_does_not_split_or_rewrite_body():
    value = {"chapters": [{"chapter_id": "C1", "summary": "摘要", "markdown": "前段。\n\n后段。",
        "issues": [{"kind": "source_issue", "quote": '原文"符号"', "reason": "有疑问", "evidence_ids": ["E1"]}]}]}
    assert parse_chapters(model_content(value)) == value


@pytest.mark.parametrize('broken', ['无效返回', '```'])
def test_partial_batch_is_saved_before_repair_and_resume_only_requests_missing(tmp_path, monkeypatch, broken):
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True))
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i, "end_seconds": i + .5,
                 "text": "原文"} for i in range(3)]
    flow = DetailedVideoGeneration("partial", model, model.settings, tmp_path, evidence, 10, asyncio.Event(), lambda: None, lambda s: None, {})
    outline = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": str(i), "core_question": "问题", "topics": ["主题"], "start_anchor_id": e["id"]}
                             for i, e in enumerate(flow.anchors)], "video_summary": "概览"}
    chapters, requests = flow.assign(outline), []
    async def request(messages, **kwargs):
        sent = json.loads(messages[1]["content"])
        requests.append(sent)
        if len(requests) == 1:
            return model_content({"chapters": [
                {"chapter_id": "C1", "summary": "摘要", "markdown": "完整正文", "issues": []},
                {"chapter_id": "C2", "summary": "摘要", "markdown": "", "issues": []},
                {"chapter_id": "C3", "summary": "摘要", "markdown": "另一章正文", "issues": []}]})
        if len(requests) == 2:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(list(tmp_path.glob("blocks-v4-chapter-*.json"))) == 2
            return broken
        return model_content({"chapters": [{"chapter_id": "C2", "summary": "摘要", "markdown": "补齐正文", "issues": []}]})
    monkeypatch.setattr(model, "request", request)
    with pytest.raises(AppError):
        asyncio.run(flow.writing(chapters, "导览"))
    assert [c["chapter_id"] for c in requests[1]["chapters"]] == ["C2"]
    result = asyncio.run(flow.writing(chapters, "导览"))
    assert [c["chapter_id"] for c in result] == ["C1", "C2", "C3"]
    assert [c["chapter_id"] for c in requests[-1]["chapters"]] == ["C2"]
