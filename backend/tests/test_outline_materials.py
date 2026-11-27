import asyncio
from copy import deepcopy
import json

import pytest

from app.analysis.content_types import CONTENT_TYPES
from app.config import Settings
from app.errors import AppError, TaskPaused
from app.videos.detailed import DetailedVideoGeneration
from app.videos.model import ModelClient
from app.videos.segments import SEGMENT_SECONDS, outline_overhead
from .fakes import FakeModel, run_model


def test_outline_speech_and_frame_ids_restore_boundaries_after_repair(tmp_path, monkeypatch):
    model = ModelClient(Settings(llm_stream=False))
    evidence = [
        {"id": "title", "kind": "frame", "start_seconds": 0, "end_seconds": 0, "text": "标题"},
        {"id": "speech", "kind": "speech", "start_seconds": 0, "end_seconds": 2, "text": "语音原文"},
        {"id": "new-slide", "kind": "frame", "start_seconds": 5, "end_seconds": 6, "text": "新画面\n第二行"},
        {"id": "later-speech", "kind": "speech", "start_seconds": 10, "end_seconds": 12, "text": "下一个问题"},
    ]
    coordinator = DetailedVideoGeneration("aliases", model, model.settings, tmp_path, evidence, 20,
        asyncio.Event(), lambda: None, lambda _: None, {})
    payload = {"evidence": coordinator.evidence, "anchors": coordinator.anchor_payload(), "context": {}}
    before, captured = deepcopy(payload), []
    chapter = {"title": "主题", "core_question": "问题", "topics": ["要点"]}
    good = {"content_type": CONTENT_TYPES["课程或教材"], "video_summary": "主题导览", "chapters": [
        chapter, {**chapter, "start_anchor_id": "F2"}, {**chapter, "start_anchor_id": "S2"}]}
    bad = deepcopy(good)
    bad["chapters"][1]["start_anchor_id"] = "F99"
    outputs = iter((bad, good))

    async def request(messages, **kwargs):
        captured.append(deepcopy(messages))
        return json.dumps(next(outputs), ensure_ascii=False)

    monkeypatch.setattr(model, "request", request)
    result = run_model(model, "generate", "outline", payload,
                       lambda value: coordinator.validate("outline", payload, value))
    assert len(captured) == 2
    sent = json.loads(captured[0][-1]["content"])
    assert sent["evidence"] == [
        {"id": "F1", "text": "标题"}, {"id": "S1", "text": "语音原文"},
        {"id": "F2", "text": "新画面 第二行"}, {"id": "S2", "text": "下一个问题"}]
    assert "anchors" not in sent and "start_seconds" not in captured[0][-1]["content"]
    assert [chapter["start_anchor_id"] for chapter in result["chapters"]] == ["E1", "E3", "E4"]
    assert coordinator.validate("outline", payload, result) == result
    assigned = coordinator.assign(result)
    assert [(chapter["start_seconds"], chapter["end_seconds"]) for chapter in assigned] == [(0, 5), (5, 10), (10, 20)]
    assert [coordinator.original[item["id"]]["id"] for chapter in assigned for item in chapter["primary"]] == [
        item["id"] for item in evidence]
    assert payload == before


@pytest.mark.parametrize("pause", [False, True])
def test_outline_context_carries_every_previous_chapter_and_resumes(tmp_path, pause):
    captured, bodies = [], []

    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.calls += 1
                sent = json.loads(self.messages(operation, payload)[-1]["content"])
                captured.append(deepcopy(sent))
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "video_summary": "本段导览",
                    "chapters": [{"title": f"主题{index}", "core_question": f"问题{index}", "topics": [f"要点{index}"],
                                  "start_anchor_id": item["id"]} for index, item in enumerate(sent["evidence"])]})
            if operation == "chapters":
                bodies.extend(deepcopy(payload["chapters"]))
            return await super().generate(operation, payload, validate)

    evidence = [{"id": f"original-{index}", "kind": "frame" if index % 2 else "speech",
                 "start_seconds": seconds, "end_seconds": seconds, "text": f"原文{index}"}
                for index, seconds in enumerate((0, SEGMENT_SECONDS - 1, SEGMENT_SECONDS, SEGMENT_SECONDS + 100))]
    model = Model(Settings())

    def publish(_):
        nonlocal pause
        if pause:
            pause = False
            raise TaskPaused()

    coordinator = DetailedVideoGeneration("context", model, model.settings, tmp_path, evidence, SEGMENT_SECONDS + 600,
        asyncio.Event(), lambda: None, lambda _: None, {}, publish=publish)
    if pause:
        with pytest.raises(TaskPaused):
            asyncio.run(coordinator.run())
    result = asyncio.run(coordinator.run())
    assert len(captured) == 2 and captured[0]["context"] == {}
    assert captured[1]["context"] == {"video_summary": "本段导览", "chapters": [
        {"title": f"主题{index}", "core_question": f"问题{index}", "topics": [f"要点{index}"]} for index in range(2)]}
    assert [item["id"] for item in captured[1]["evidence"]] == ["S1", "F1"]
    assert "start_anchor_id" not in json.dumps(captured[1]["context"])
    assert bodies[2]["context"][0]["text"] == "原文1"
    assert bodies[2]["previous_chapter_outline"]["title"] == "主题1"
    assert [source for chapter in result["chapters"] for source in chapter["evidence_ids"]] == [item["id"] for item in evidence]
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls


def test_next_segment_counts_actual_previous_outline_without_extracting(tmp_path):
    requests = []

    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            requests.append((operation, deepcopy(payload)))
            if operation == "outline":
                self.calls += 1
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "video_summary": "本段概要",
                    "chapters": [{"title": "主题", "core_question": "核心问题", "topics": ["重要要点" * 200]}]})
            return await super().generate(operation, payload, validate)

    model = Model(Settings())
    model.settings.llm_input_text_limit = outline_overhead(model) + 4500
    evidence = [{"id": f"original-{index}", "kind": "speech", "start_seconds": seconds,
                 "end_seconds": seconds + 1, "text": "原" * (100 if seconds < SEGMENT_SECONDS else 1400)}
                for index, seconds in enumerate((0, SEGMENT_SECONDS - 1, SEGMENT_SECONDS,
                                                  SEGMENT_SECONDS + 100, SEGMENT_SECONDS + 200))]
    coordinator = DetailedVideoGeneration("actual-budget", model, model.settings, tmp_path, evidence, SEGMENT_SECONDS + 600,
        asyncio.Event(), lambda: None, lambda _: None, {})
    result = asyncio.run(coordinator.run())
    outlines = [payload for operation, payload in requests if operation == "outline"]
    assert len(outlines) == 3 and all(operation != "extract" for operation, _ in requests)
    assert all(model.input_characters("outline", payload) + model.repair_input_reserve <= model.settings.llm_input_text_limit
               for payload in outlines)
    assert [item["id"] for payload in outlines for item in payload["evidence"]] == [f"E{index}" for index in range(1, 6)]
    assert result["total_segments"] == result["completed_segments"] == 3
    assert len(json.loads(next(tmp_path.glob("blocks-v4-segments-*.json")).read_text("utf-8"))) == 3
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls


@pytest.mark.parametrize("case", ["parallel", "serial", "pause", "outline_error"])
def test_next_outline_shares_slots_with_current_body_and_resumes_in_order(tmp_path, case):
    async def run():
        body_started, outline_started = asyncio.Event(), asyncio.Event()
        release_body, release_outline = asyncio.Event(), asyncio.Event()
        stop = asyncio.Event()
        active, peak, published, requests = 0, 0, [], []
        first_attempt = True

        class Model(FakeModel):
            def input_characters(self, operation, payload):
                if operation == "chapters" and len(payload["chapters"]) > 1:
                    return 1000000  # 两章分别请求，和下一段大纲竞争并发名额。
                return super().input_characters(operation, payload)

            async def generate(self, operation, payload, validate):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    if operation == "outline":
                        ids = [item["id"] for item in payload["anchors"]]
                        requests.append(ids[0])
                        if payload["context"]:
                            assert [c["title"] for c in payload["context"]["chapters"]] == ["E1", "E2"]
                            outline_started.set()
                            await release_outline.wait()
                            if first_attempt and case == "outline_error":
                                raise AppError("model_network_error", "模拟后续大纲失败")
                        return validate({"content_type": CONTENT_TYPES["课程或教材"], "video_summary": "本段概要",
                            "chapters": [{"title": i, "core_question": "问题", "topics": ["要点"],
                                          "start_anchor_id": i} for i in ids]})
                    if operation == "chapters":
                        chapter = payload["chapters"][0]["chapter_id"]
                        requests.append(chapter)
                        if chapter in {"C1", "C2"}:
                            body_started.set()
                            await release_body.wait()
                        else:
                            assert published[-1] == 1
                    return await super().generate(operation, payload, validate)
                finally:
                    active -= 1

        model = Model(Settings(llm_section_concurrency=1 if case == "serial" else 2))
        model.settings.llm_input_text_limit = outline_overhead(model) + 6000
        evidence = [{"id": str(i), "kind": "speech", "start_seconds": i * 100, "end_seconds": i * 100 + 1,
                     "text": "字" * 2500} for i in range(3)]
        flow = DetailedVideoGeneration("overlap", model, model.settings, tmp_path, evidence, 300, stop,
            lambda: None, lambda _: None, {}, publish=lambda note: published.append(note["completed_segments"]))
        task = asyncio.create_task(flow.run())
        await body_started.wait()
        if case == "serial":
            assert not outline_started.is_set()
            release_body.set()
        await outline_started.wait()
        assert published == ([1] if case == "serial" else [])
        assert active == model.settings.llm_section_concurrency
        if case == "pause":
            stop.set()
            with pytest.raises(TaskPaused):
                await task
            assert active == 0 and published == []
            stop.clear()
        release_outline.set()
        release_body.set()
        if case == "outline_error":
            with pytest.raises(AppError, match="模拟后续大纲失败"):
                await task
            assert active == 0 and published == [1]
        if case in {"pause", "outline_error"}:
            first_attempt = False
            task = asyncio.create_task(flow.run())
        result = await task
        assert result["complete"] and [c["chapter_id"] for c in result["chapters"]] == ["C1", "C2", "C3"]
        assert requests.count("E1") == 1 and requests.count("E3") == (2 if case in {"pause", "outline_error"} else 1)
        if case != "pause":
            assert requests.count("C1") == requests.count("C2") == 1
        assert active == 0 and peak == model.settings.llm_section_concurrency

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_pending_extraction_can_use_all_slots_after_prior_body_is_published(tmp_path):
    async def run():
        first_extract, two_extracts = asyncio.Event(), asyncio.Event()
        published, extracting = [], 0

        class Model(FakeModel):
            async def generate(self, operation, payload, validate):
                nonlocal extracting
                if operation == "extract":
                    extracting += 1
                    try:
                        first_extract.set()
                        if extracting == 2:
                            assert published == [1]
                            two_extracts.set()
                        await two_extracts.wait()
                        return validate({"facts": [{"text": "本段事实"}], "issues": []})
                    finally:
                        extracting -= 1
                if operation == "chapters" and payload["chapters"][0]["chapter_id"] == "C1":
                    await first_extract.wait()
                return await super().generate(operation, payload, validate)

        model = Model(Settings(llm_section_concurrency=2, llm_input_text_limit=9000))
        evidence = [{"id": str(i), "kind": "speech", "start_seconds": i * 100, "end_seconds": i * 100 + 1,
                     "text": text} for i, text in enumerate(("第一章", "长原文" * 10000))]
        flow = DetailedVideoGeneration("extraction", model, model.settings, tmp_path, evidence, 300,
            asyncio.Event(), lambda: None, lambda _: None, {}, publish=lambda note: published.append(note["completed_segments"]))
        assert (await flow.run())["complete"]
        assert two_extracts.is_set() and published == [1, 2]

    asyncio.run(asyncio.wait_for(run(), timeout=10))
