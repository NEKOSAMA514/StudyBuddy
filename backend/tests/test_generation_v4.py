from app.analysis.budgets import message_characters
from app.analysis.content_types import CONTENT_TYPES
import asyncio
from copy import deepcopy
import json
import pytest
from app.videos.detailed import DetailedVideoGeneration
from app.errors import TaskPaused
from app.analysis.budgets import MAX_BATCH_CHAPTERS
from app.videos.notes import notes_markdown, notes_text
from app.config import Settings
from app.errors import AppError
from .fakes import FakeModel


def sources(count=12, text_size=250, kind="speech"):
    return [{"id": f"source-{i}", "kind": kind, "start_seconds": i,
             "end_seconds": i + .5 if kind == "speech" else i,
             "text": f"主题{i}：" + "".join(f"第{i}-{j}项完整材料。" for j in range(text_size))} for i in range(count)]


def plan(anchors):
    return [{"title": i["id"], "core_question": "核心问题", "topics": ["子主题"], "start_anchor_id": i["id"]}
            for i in anchors]


class PlannerModel(FakeModel):
    def __init__(self, settings):
        super().__init__(settings)
        self.inputs = []

    async def generate(self, operation, payload, validate):
        self.inputs.append((operation, deepcopy(payload)))
        if operation == "outline":
            self.calls += 1
            return validate({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(payload["anchors"]), "video_summary": "介绍各主题的主要内容。"})
        return await super().generate(operation, payload, validate)


def flow(tmp_path, model_type=PlannerModel, evidence=None, budget=12000):
    model = model_type(Settings(llm_input_text_limit=budget))
    coordinator = DetailedVideoGeneration("v4", model, model.settings, tmp_path, evidence or sources(), 30,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    return coordinator, model


def test_short_material_plans_once_without_merge(tmp_path):
    coordinator, model = flow(tmp_path, evidence=sources(2, 2))
    asyncio.run(coordinator.planning())
    assert [op for op, _ in model.inputs] == ["outline"]


def test_current_batch_cap_splits_one_extra_chapter_without_losing_structure(tmp_path):
    coordinator, model = flow(tmp_path, evidence=sources(MAX_BATCH_CHAPTERS + 1, 1))
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(coordinator.anchors), "video_summary": "主题"})
    drafts = asyncio.run(coordinator.writing(chapters, "主题"))
    assert [len(payload["chapters"]) for operation, payload in model.inputs] == [MAX_BATCH_CHAPTERS, 1]
    assert [chapter["chapter_id"] for chapter in drafts] == [chapter["chapter_id"] for chapter in chapters]


def test_planning_character_limit_includes_prompt_before_any_request(tmp_path):
    original = sources(1, 1)
    original[0]["text"] = "中" * 15000
    coordinator, model = flow(tmp_path, evidence=original, budget=1000)
    model.settings.summary_prompt = "用中文梳理主题。" * 1000
    with pytest.raises(AppError) as error:
        asyncio.run(coordinator.planning())
    assert error.value.code == "model_input_budget" and not model.inputs


@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("failure", ["invalid_quote", "network", "invalid_source"])
def test_bad_auxiliary_quote_keeps_body_and_only_verified_quote_is_exported(tmp_path, critical, failure):
    class Model(PlannerModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                value = await super().generate(operation, payload, validate)
                value["chapters"][0]["issues"] = [{"kind": "source_issue", "quote": "并不存在的引文",
                    "reason": "这个事实尚无法确认", "critical": critical,
                    "evidence_ids": ["missing" if failure == "invalid_source" else "E1"]}]
                return value
            if operation == "issue_quotes":
                assert list(tmp_path.glob("blocks-v4-chapter-*.json"))
                raise AppError("model_network_error" if failure == "network" else "invalid_model_output", "模拟失败")
            return await super().generate(operation, payload, validate)
    coordinator, _ = flow(tmp_path, Model, sources(1, 2))
    result = asyncio.run(coordinator.run())
    assert result["chapters"][0]["markdown"] == "窗口在慢启动阶段增长。"
    assert len(result["source_issues"]) == int(critical)
    if critical:
        assert result["source_issues"][0]["quote"] is None
        assert result["source_issues"][0]["evidence_ids"] == ["source-0"]
    for export in (notes_text, notes_markdown):
        text = export("视频", 30, result)
        assert "并不存在的引文" not in text and "需核查" not in text
        assert ("这个事实尚无法确认" in text) == critical


def test_batching_uses_output_budget_without_limiting_writing(tmp_path, monkeypatch):
    coordinator, model = flow(tmp_path, evidence=sources(5, 10), budget=24000)
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(coordinator.anchors), "video_summary": "主题"})
    # 最多可合五章，输出预算只容纳两章时仍应拆批。
    monkeypatch.setitem(model.settings.llm_output_tokens, "chapters", 2500)
    asyncio.run(coordinator.writing(chapters, "主题"))
    batches = [p["chapters"] for op, p in model.inputs if op == "chapters"]
    assert list(map(len, batches)) == [2, 2, 1]


def test_actual_batch_truncation_splits_without_rewriting_saved_chapters(tmp_path):
    class Model(PlannerModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters" and len(payload["chapters"]) > 1:
                self.inputs.append((operation, deepcopy(payload)))
                raise AppError("model_output_truncated", "截断")
            return await super().generate(operation, payload, validate)
    coordinator, model = flow(tmp_path, Model, sources(5, 2))
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(coordinator.anchors), "video_summary": "内容"})
    result = asyncio.run(coordinator.writing(chapters, "内容"))
    assert len(result) == 5 and [c["chapter_id"] for c in result] == ["C1", "C2", "C3", "C4", "C5"]
    count = len(model.inputs)
    assert asyncio.run(coordinator.writing(chapters, "内容")) == result and len(model.inputs) == count
    assert all(payload["video_summary"] == "内容" for operation, payload in model.inputs if operation == "chapters")


def test_body_batches_share_summary_and_summary_changes_do_not_reuse_old_body(tmp_path, monkeypatch):
    monkeypatch.setattr("app.videos.detailed.MAX_BATCH_CHAPTERS", 5)
    coordinator, model = flow(tmp_path, evidence=sources(7, 1))
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(coordinator.anchors), "video_summary": "主题"})
    summary = "比较各模型的整体表现，技术细节服务于主要结论。"
    result = asyncio.run(coordinator.writing(chapters, summary))
    assert [len(payload["chapters"]) for operation, payload in model.inputs] == [5, 2]
    for operation, payload in model.inputs:
        assert operation == "chapters" and payload["video_summary"] == summary
        assert all("video_summary" not in chapter for chapter in payload["chapters"])
        sent = json.loads(model.messages(operation, payload)[-1]["content"])
        assert sent["video_summary"] == summary
        assert model.input_characters(operation, payload) == message_characters(model.messages(operation, payload))
    calls = model.calls
    assert asyncio.run(coordinator.writing(chapters, summary)) == result and model.calls == calls
    asyncio.run(coordinator.writing(chapters, "不同的主题方向"))
    assert model.calls == calls + 2
    assert asyncio.run(coordinator.writing(chapters, summary)) == result and model.calls == calls + 2


def test_nested_split_markers_resume_without_repeating_truncated_batches(tmp_path, monkeypatch):
    monkeypatch.setattr("app.videos.detailed.MAX_BATCH_CHAPTERS", 4)
    requests, pause = [], True
    class Model(PlannerModel):
        async def generate(self, operation, payload, validate):
            nonlocal pause
            if operation == "chapters":
                ids = tuple(c["chapter_id"] for c in payload["chapters"])
                requests.append((ids, payload["video_summary"]))
                if len(ids) > 1:
                    raise AppError("model_output_truncated", "测试截断")
                if pause:
                    pause = False
                    raise TaskPaused()
            return await super().generate(operation, payload, validate)
    coordinator, model = flow(tmp_path, Model, sources(4, 1))
    model.settings.llm_section_concurrency = 1
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plan(coordinator.anchors), "video_summary": "主题"})
    with pytest.raises(TaskPaused):
        asyncio.run(coordinator.writing(chapters, "主题"))
    truncated = {ids for ids, _ in requests if len(ids) > 1}
    assert len(truncated) >= 2
    before = len(requests)
    result = asyncio.run(coordinator.writing(chapters, "主题"))
    assert len(result) == 4
    assert not truncated.intersection(ids for ids, _ in requests[before:])
    assert all(summary == "主题" for _, summary in requests)
    before = len(requests)
    asyncio.run(coordinator.writing(chapters, "新的主题"))
    assert len(requests) > before and all(summary == "新的主题" for _, summary in requests[before:])


def test_minor_quote_repair_success_keeps_source_without_resending_body(tmp_path):
    class Model(PlannerModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                value = await super().generate(operation, payload, validate)
                value["chapters"][0]["issues"] = [{"kind": "source_issue", "quote": "改写文字", "reason": "名称不清",
                    "evidence_ids": ["E1"], "critical": False}]
                return value
            if operation == "issue_quotes":
                self.inputs.append((operation, deepcopy(payload)))
                assert "markdown" not in json.dumps(payload)
                return validate({"repairs": [{"chapter_id": "C1", "block_index": 0, "quote": "完整材料"}]})
            return await super().generate(operation, payload, validate)
    coordinator, model = flow(tmp_path, Model, sources(1, 2))
    result = asyncio.run(coordinator.run())
    assert result["source_issues"][0]["quote"] == "完整材料"
    count = len(model.inputs)
    assert asyncio.run(coordinator.run()) == result and len(model.inputs) == count


def test_stop_during_quote_repair_preserves_body_checkpoint(tmp_path):
    started, cancelled = asyncio.Event(), []
    class Model(PlannerModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                value = await super().generate(operation, payload, validate)
                value["chapters"][0]["issues"] = [{"kind": "source_issue", "quote": "改写文字", "reason": "名称不清",
                                                   "evidence_ids": ["E1"], "critical": True}]
                return value
            if operation == "issue_quotes":
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.append(operation)
                    raise
            return await super().generate(operation, payload, validate)
    coordinator, _ = flow(tmp_path, Model, sources(1, 2))
    async def run():
        task = asyncio.create_task(coordinator.run())
        await asyncio.wait_for(started.wait(), 1)
        coordinator.stop.set()
        with pytest.raises(TaskPaused):
            await asyncio.wait_for(task, 2)
    asyncio.run(run())
    assert cancelled == ["issue_quotes"]
    saved = json.loads(next(tmp_path.glob("blocks-v4-chapter-*.json")).read_text(encoding="utf-8"))
    assert saved["markdown"] == "窗口在慢启动阶段增长。"
    assert not list(tmp_path.glob("blocks-v4-overview-*.json"))
