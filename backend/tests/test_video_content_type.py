"""Verify type guidance in actual video model requests, checkpoints and results."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import httpx
import pytest

from app.analysis.content_types import CONTENT_TYPES
from app.config import Settings
from app.videos.detailed import DetailedVideoGeneration
from app.errors import TaskPaused
from app.videos.model import ModelClient
from app.videos.schemas import Outline
from .fakes import model_content, outline_result


@asynccontextmanager
async def video_flow(folder, selected="课程或教材", name_only=True, hook=lambda payload, value: value,
                     text="原文条件与过程。", segments=1):
    calls, snapshots = [], []
    settings = Settings(llm_key="fake-content-type", llm_stream=False, llm_input_text_limit=12000)
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i * 1200,
                 "end_seconds": i * 1200 + 10, "text": text} for i in range(segments)]
    original = deepcopy(evidence)

    async def respond(request):
        messages = json.loads(request.content)["messages"]
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user" and m["content"].startswith("{")))
        operation = next((op for key, op in (("evidence", "outline"), ("chapter", "extract"),
                         ("chapters", "chapters"), ("repairs", "issue_quotes")) if key in payload), "overview")
        calls.append((operation, payload, messages))
        if operation == "outline":
            value = {**outline_result(payload["evidence"][0]["id"]),
                     "content_type": selected if name_only else CONTENT_TYPES[selected]}
        elif operation == "extract":
            chapter = payload["chapter"]
            value = {"facts": [{"text": item["text"][:40]} for item in chapter.get("primary", chapter.get("facts", []))], "issues": []}
        elif operation == "chapters":
            value = {"chapters": [{"chapter_id": c["chapter_id"], "summary": "主要过程与条件。", "markdown": "保留原文条件与过程。",
                     "issues": [{"quote": "不存在的引文", "reason": "条件不明确", "evidence_ids": [c["start_anchor_id"]], "critical": True}]}
                     for c in payload["chapters"]]}
        elif operation == "issue_quotes":
            value = {"repairs": [{"chapter_id": r["chapter_id"], "block_index": r["block_index"], "quote": payload["sources"][0]["text"][:5]}
                                  for r in payload["repairs"]]}
        else:
            value = {"title": "原文主线", "tags": ["过程"], "overview_paragraphs": ["归纳主要内容。"]}
        value = hook(payload, value)
        return httpx.Response(200, json={"choices": [{"message": {"content": model_content(value)}, "finish_reason": "stop"}]})

    async with ModelClient(settings) as model:
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

        def coordinator():
            return DetailedVideoGeneration("type-test", model, settings, folder, evidence, segments * 1200,
                                  asyncio.Event(), lambda: None, lambda _: None, {},
                                  publish=lambda result: snapshots.append(deepcopy(result)), video_title="测试视频")
        yield coordinator, model, calls, snapshots
    assert evidence == original


@pytest.mark.parametrize("selected", CONTENT_TYPES)
@pytest.mark.parametrize("name_only", [True, False])
def test_video_type_and_full_guidance_reach_all_later_requests(tmp_path, selected, name_only):
    async def run():
        async with video_flow(tmp_path, selected, name_only, segments=2) as (make, _, calls, snapshots):
            result = await make().run()
            assert [op for op, _, _ in calls] == ["outline", "chapters", "issue_quotes"] * 2 + ["overview"]
            assert "content_type" not in calls[0][1]
            assert all(payload["content_type"] == CONTENT_TYPES[selected] for _, payload, _ in calls[1:])
            assert all(value["content_type"] == CONTENT_TYPES[selected] for value in [*snapshots, result])
            assert all(value["complete"] is False for value in snapshots) and result["complete"]
            count = len(calls)
            cached = make()
            assert await cached.run() == result and len(calls) == count
            assert await cached.run() == result and len(calls) == count
    asyncio.run(run())


def test_partial_video_resume_restores_selected_type_without_replanning_finished_segment(tmp_path):
    async def run():
        async with video_flow(tmp_path, "资讯或事件记录", segments=2) as (make, _, calls, snapshots):
            first = make()
            publish = first.publish
            def stop_after_segment(value):
                publish(value)
                first.stop.set()
            first.publish = stop_after_segment
            with pytest.raises(TaskPaused):
                await first.run()
            assert len(snapshots) == 1
            completed = deepcopy(snapshots[0]["chapters"])
            calls.clear()
            result = await make().run()
            assert [op for op, _, _ in calls] == ["outline", "chapters", "issue_quotes", "overview"]
            assert all(p["content_type"] == CONTENT_TYPES["资讯或事件记录"] for _, p, _ in calls)
            assert result["chapters"][:len(completed)] == completed
    asyncio.run(run())


def test_continuation_type_change_uses_existing_structural_repair(tmp_path):
    changed = False
    def change_once(payload, value):
        nonlocal changed
        if "evidence" in payload and payload.get("content_type") and not changed:
            changed = True
            return {**value, "content_type": "经验分享"}
        return value
    async def run():
        async with video_flow(tmp_path, "操作教程", hook=change_once, segments=2) as (make, _, calls, _):
            result = await make().run()
            outlines = [(p, m) for op, p, m in calls if op == "outline"]
            assert len(outlines) == 3 and changed
            assert outlines[1][0] == outlines[2][0]
            assert "content_type_must_match_planned_video_type" in outlines[2][1][-1]["content"]
            assert result["content_type"] == CONTENT_TYPES["操作教程"]
    asyncio.run(run())


@pytest.mark.parametrize("before_outline", [False, True])
def test_type_is_metered_and_preserved_during_long_material_extraction(tmp_path, before_outline):
    async def run():
        def longer_guide(payload, value):
            if "evidence" in payload and not before_outline:
                return {**value, "video_summary": "主题与条件。" * 600}
            if "chapters" in payload:
                return {"chapters": [{**c, "issues": []} for c in value["chapters"]]}
            return value
        async with video_flow(tmp_path, text="原文条件。" * (6000 if before_outline else 1500), hook=longer_guide) as (make, model, calls, _):
            coordinator = make()
            result = await coordinator.run()
            assert any(op == "extract" for op, _, _ in calls)
            planned = False
            for op, payload, _ in calls:
                if op == "outline":
                    planned = True
                elif planned:
                    assert payload["content_type"] == CONTENT_TYPES["课程或教材"]
                else:
                    assert op == "extract" and before_outline and "content_type" not in payload
                if op in {"outline", "extract", "chapters"} and "content_type" in payload:
                    without_type = {k: v for k, v in payload.items() if k != "content_type"}
                    assert model.input_characters(op, payload) - model.input_characters(op, without_type) == len(payload["content_type"])
            assert result["complete"]
    asyncio.run(run())


@pytest.mark.parametrize("value", ["未定义类型", "课程或教材：自行改写的要求", "操作教程：", ""])
def test_video_outline_rejects_unknown_or_rewritten_type(value):
    with pytest.raises(ValueError, match="content_type_must_be_a_supported_type"):
        Outline.model_validate({**outline_result("E1"), "content_type": value})
