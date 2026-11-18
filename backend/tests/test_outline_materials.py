import asyncio
from copy import deepcopy
import json

import pytest

from app.analysis.content_types import CONTENT_TYPES
from app.config import Settings
from app.errors import TaskPaused
from app.videos.detailed import DetailedVideoGeneration
from app.videos.model import ModelClient
from app.videos.segments import outline_overhead
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
                for index, seconds in enumerate((0, 1199, 1200, 1300))]
    model = Model(Settings())

    def publish(_):
        nonlocal pause
        if pause:
            pause = False
            raise TaskPaused()

    coordinator = DetailedVideoGeneration("context", model, model.settings, tmp_path, evidence, 1800,
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
                 "end_seconds": seconds + 1, "text": "原" * (100 if seconds < 1200 else 1400)}
                for index, seconds in enumerate((0, 1199, 1200, 1300, 1400))]
    coordinator = DetailedVideoGeneration("actual-budget", model, model.settings, tmp_path, evidence, 1800,
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
