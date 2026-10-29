from app.analysis.budgets import message_characters
from app.analysis.content_types import CONTENT_TYPES
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app.videos.detailed import DetailedVideoGeneration
from app.errors import TaskPaused
from app.videos.segments import split_segments, preceding_context
from app.analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT
from app.videos.notes import notes_text, notes_markdown
from app.videos.pipeline import Pipeline
from app.recognition.extraction import extraction_config, extraction_signature
from app.config import Settings
from app.db import Evidence, Task, Video
from app.errors import AppError
from app.videos.serialization import task_progress
from .conftest import upload
from .fakes import FakeModel, FakeRecognizer


def sources(count=3):
    return [{"id": f"s{i}", "kind": "speech", "start_seconds": i * 1200,
             "end_seconds": i * 1200 + 10, "text": f"第{i}段的新增内容。"} for i in range(count)]


class RecordingModel(FakeModel):
    def __init__(self, settings):
        super().__init__(settings)
        self.inputs = []

    async def generate(self, operation, payload, validate):
        self.inputs.append((operation, deepcopy(payload)))
        return await super().generate(operation, payload, validate)


def flow(tmp_path, evidence=None, model_type=RecordingModel, **kwargs):
    model = model_type(Settings(llm_input_text_limit=24000, llm_section_concurrency=4))
    snapshots, progress = [], []
    coordinator = DetailedVideoGeneration("segments-test", model, model.settings, tmp_path, evidence or sources(),
        kwargs.pop("duration", 3600), asyncio.Event(), lambda: None, lambda stage: None, {},
        publish=lambda value: snapshots.append(deepcopy(value)), segment_update=lambda value: progress.append(deepcopy(value)), **kwargs)
    return coordinator, model, snapshots, progress


@pytest.mark.parametrize("seconds,characters,segmented", [(1200, DEFAULT_INPUT_TEXT_LIMIT, False), (1201, DEFAULT_INPUT_TEXT_LIMIT, True),
    (1200, DEFAULT_INPUT_TEXT_LIMIT + 1, True), (1199, DEFAULT_INPUT_TEXT_LIMIT - 1, False)])
def test_segment_trigger_boundaries(seconds, characters, segmented):
    evidence = sources(2)
    evidence[1].update(start_seconds=600, end_seconds=601)
    evidence[0]["text"], evidence[1]["text"] = "甲" * (characters // 2), "乙" * (characters - characters // 2)
    actual, parts = split_segments(evidence, seconds)
    assert actual is segmented
    assert [item["id"] for part in parts for item in part["primary"]] == ["s0", "s1"]
    assert parts[0]["start_seconds"] == 0 and parts[-1]["end_seconds"] == seconds


def test_repeated_ocr_does_not_split_short_video_or_discard_chemical_changes(tmp_path):
    repeated = "酸性条件下以铁铵矾为指示剂，使用标准溶液进行滴定。\n" * 25
    changes = ["AgCl；Fe2+；0.1mL", "AgC1；Fe3+；0.01mL", "SCN−；温度20℃", "SCN+；温度30℃"]
    evidence = [{"id": f"frame{i}", "kind": "frame", "start_seconds": i * 15,
                 "end_seconds": i * 15, "text": repeated + text} for i, text in enumerate(changes)]
    evidence = [{**item, "text": item["text"] * 12} for item in evidence]
    original = deepcopy(evidence)
    assert sum(len(item["text"]) for item in evidence) > DEFAULT_INPUT_TEXT_LIMIT
    segmented, parts = split_segments(evidence, 650)
    assert not segmented and len(parts) == 1 and parts[0]["primary"] == original
    coordinator, model, _, progress = flow(tmp_path, evidence, duration=650)
    result = asyncio.run(coordinator.run())
    assert [op for op, _ in model.inputs] == ["outline", "chapters", "overview"]
    assert not progress and not result["segment_guides"]
    chapter = result["chapters"][0]
    assert chapter["evidence_ids"] == [item["id"] for item in evidence]
    assert (chapter["start_seconds"], chapter["end_seconds"], chapter["timestamp_basis"]) == (0, 650, "frame")
    assert evidence == original
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls
    assert split_segments(evidence, 1201)[0]


def test_unique_ocr_still_counts_towards_segment_budget():
    evidence = [{"id": f"frame{i}", "kind": "frame", "start_seconds": i * 15,
                 "end_seconds": i * 15, "text": text * (DEFAULT_INPUT_TEXT_LIMIT // 2 + 1)} for i, text in enumerate(("甲", "乙", "丙"))]
    segmented, parts = split_segments(evidence, 650)
    assert segmented and len(parts) == 3
    assert [item for part in parts for item in part["primary"]] == evidence


def test_each_segment_counts_its_own_shared_ocr_and_reserves_context(small_outline_budget):
    evidence = [{"id": f"frame{i}", "kind": "frame", "start_seconds": i * 15,
                 "end_seconds": i * 15, "text": text * count}
                for i, (text, count) in enumerate((("甲", 8000), ("甲", 8000),
                                                  ("乙", 8000), ("乙", 8000), ("丙", 6000)))]
    segmented, parts = split_segments(evidence, 650)
    assert segmented and [[item["id"] for item in part["primary"]] for part in parts] == [
        ["frame0", "frame1"], ["frame2", "frame3"], ["frame4"]]
    model = FakeModel(Settings())
    for part in parts:
        payload = {"evidence": part["primary"]}
        assert model.input_characters("outline", payload) == message_characters(model.messages("outline", payload))
    assert [item for part in parts for item in part["primary"]] == evidence


def test_same_time_records_stay_together_and_silence_has_no_empty_request():
    evidence = sources(2)
    evidence[1].update(start_seconds=3000, end_seconds=3010)
    evidence.insert(1, {**evidence[0], "id": "frame", "kind": "frame", "end_seconds": 0})
    _, parts = split_segments(evidence, 7200)
    assert [[i["id"] for i in part["primary"]] for part in parts] == [["s0", "frame"], ["s1"]]
    assert [(part["start_seconds"], part["end_seconds"]) for part in parts] == [(0, 3000), (3000, 7200)]


def test_dense_material_reserves_actual_preceding_minute_context(small_outline_budget):
    evidence = [{**sources(1)[0], "id": f"s{i}", "text": "原" * size,
                 "start_seconds": i * 100, "end_seconds": i * 100 + 1}
                for i, size in enumerate([5000, 5000, 5000, 10000, 3000, 1000])]
    _, parts = split_segments(evidence, 600)
    assert [sum(len(i["text"]) for i in p["primary"]) for p in parts] == [15000, 14000]
    # 中间有一百秒间隔，不补入更早原文或生成概要。
    assert preceding_context(evidence, parts[1]["start_seconds"]) == []
    dense = [{**item, "start_seconds": i * 20, "end_seconds": i * 20 + 1} for i, item in enumerate(evidence)]
    model = FakeModel(Settings())
    _, parts = split_segments(dense, 600, 15000, lambda p: model.input_characters("outline", p))
    for part in parts:
        payload = {"evidence": part["primary"], "context": preceding_context(dense, part["start_seconds"])}
        assert model.input_characters("outline", payload) <= 15000 or len(part["primary"]) == 1
    assert [item for part in parts for item in part["primary"]] == dense


def test_minute_context_keeps_raw_asr_and_ocr_and_excludes_future_and_silence():
    evidence = [{"id": str(i), "kind": kind, "start_seconds": start, "end_seconds": end, "text": text}
                for i, (kind, start, end, text) in enumerate([
                    ("speech", 0, 10, "更早内容"), ("speech", 50, 61, "跨过窗口起点的完整原句"),
                    ("frame", 59, 59, "过早画面"), ("frame", 60, 60, "窗口起点画面"),
                    ("speech", 90, 121, "跨过章节起点的原句"), ("speech", 120, 130, "本章原文"),
                    ("frame", 130, 130, "后续画面")])]
    before = deepcopy(evidence)
    assert [item["id"] for item in preceding_context(evidence, 120)] == ["1", "3", "4"]
    assert preceding_context(evidence, 0) == []
    assert preceding_context(evidence, 300) == []
    assert evidence == before


def test_chapter_context_and_previous_outline_are_separate_from_primary_and_anchors(tmp_path):
    times = [0, 59, 60, 119, 120, 179, 180, 239, 240]
    evidence = [{"id": f"s{i}", "kind": "speech" if i % 2 == 0 else "frame",
                 "start_seconds": seconds, "end_seconds": seconds, "text": f"原文{i}"}
                for i, seconds in enumerate(times)]
    coordinator, model, _, _ = flow(tmp_path, evidence, duration=300)
    plans = [{"title": f"第{i}章", "core_question": f"问题{i}", "topics": [f"子主题{i}"],
              "start_anchor_id": f"E{index + 1}"} for i, index in enumerate([0, 4, 6, 8])]
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plans, "video_summary": "导览"})
    assert [[item["id"] for item in c["context"]] for c in chapters] == [[], ["E3", "E4"], ["E5", "E6"], ["E7", "E8"]]
    for index, chapter in enumerate(chapters):
        assert not {item["id"] for item in chapter["primary"]} & {item["id"] for item in chapter["context"]}
        assert chapter["previous_chapter_outline"] == ({key: plans[index - 1][key] for key in ("title", "core_question", "topics")} if index else {})
        sent = json.loads(model.messages("chapters", {"chapters": [chapter]})[-1]["content"])["chapters"][0]
        assert "start_seconds" not in json.dumps(sent)
        assert [item["id"] for item in sent["primary"]] == [item["id"] for item in chapter["primary"]]
        assert [item["id"] for item in sent["context"]] == [item["id"] for item in chapter["context"]]
    assert [item for c in chapters for item in c["primary"]] == coordinator.evidence


def test_minute_context_and_previous_outline_cross_segments_without_owning_old_evidence(tmp_path):
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": seconds,
                 "end_seconds": seconds + 1, "text": f"原始证据{i}"}
                for i, seconds in enumerate([0, 1139, 1140, 1199, 1200])]
    coordinator, model, snapshots, _ = flow(tmp_path, evidence, duration=1800)
    before = deepcopy(evidence)
    result = asyncio.run(coordinator.run())
    outline = [p for op, p in model.inputs if op == "outline"][1]
    chapter = [p["chapters"][0] for op, p in model.inputs if op == "chapters"][1]
    assert [item["id"] for item in outline["evidence"]] == ["E5"]
    assert [item["id"] for item in outline["context"]] == ["E3", "E4"]
    assert [item["id"] for item in outline["anchors"]] == ["E5"]
    assert chapter["primary"] == outline["evidence"] and chapter["context"] == outline["context"]
    assert chapter["previous_chapter_outline"] == {"title": "慢启动", "core_question": "拥塞窗口如何增长？", "topics": ["拥塞窗口增长"]}
    assert result["chapters"][1]["evidence_ids"] == ["s4"]
    assert len(snapshots) == 2 and evidence == before
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls


def test_extraction_batches_use_their_own_minute_context_and_keep_previous_outline(tmp_path):
    class Model(RecordingModel):
        async def generate(self, operation, payload, validate):
            if operation == "extract":
                self.inputs.append((operation, deepcopy(payload)))
                self.calls += 1
                return validate({"facts": [{"text": "提取内容"}], "issues": []})
            return await super().generate(operation, payload, validate)
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": seconds,
                 "end_seconds": seconds + 1, "text": text}
                for i, (seconds, text) in enumerate([(0, "开头"), (50, "前文原句"), (100, "甲" * 3000), (170, "乙" * 3000)])]
    coordinator, model, _, _ = flow(tmp_path, evidence, Model, duration=200)
    model.settings.llm_input_text_limit = 4000
    plans = [{"title": "前章", "core_question": "前问", "topics": ["前提"], "start_anchor_id": "E1"},
             {"title": "本章", "core_question": "本问", "topics": ["本题"], "start_anchor_id": "E3"}]
    chapter = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plans, "video_summary": "导览"})[1]
    assert asyncio.run(coordinator.writing([chapter], "导览"))[0]["chapter_id"] == "C2"
    fragments = {"E3": "", "E4": ""}
    for operation, payload in model.inputs:
        assert model.input_characters(operation, payload) <= 4000
        assert payload["video_summary"] == "导览"
        request = payload["chapter"] if operation == "extract" else payload["chapters"][0]
        assert request["previous_chapter_outline"] == {"title": "前章", "core_question": "前问", "topics": ["前提"]}
        if operation == "extract":
            item = request["primary"][0]
            fragments[item["id"]] += item["text"]
            assert [i["id"] for i in request["context"]] == (["E2"] if item["id"] == "E3" else [])
    assert fragments == {"E3": "甲" * 3000, "E4": "乙" * 3000}


def test_oversized_single_record_is_extracted_inside_its_segment_without_loss(tmp_path, small_outline_budget):
    class Model(RecordingModel):
        async def generate(self, operation, payload, validate):
            if operation == "extract":
                self.inputs.append((operation, deepcopy(payload)))
                self.calls += 1
                return validate({"facts": [{"text": "提取的重要解释与结论。"}], "issues": []})
            return await super().generate(operation, payload, validate)
    evidence = sources(2)
    evidence[0]["text"] = "完整原文" * 5000 + "末尾条件"
    coordinator, model, snapshots, _ = flow(tmp_path, evidence, Model, duration=2400)
    model.settings.llm_input_text_limit = 15000
    result = asyncio.run(coordinator.run())
    fragments = [item["text"] for op, payload in model.inputs if op == "extract"
                 for item in payload["chapter"]["primary"]]
    assert "".join(fragments) == evidence[0]["text"]
    assert [chapter["evidence_ids"] for chapter in result["chapters"]] == [["s0"], ["s1"]]
    assert len(snapshots) == 2
    outline_inputs = [payload for op, payload in model.inputs if op == "outline"]
    assert all(model.input_characters("outline", payload) <= 15000 for payload in outline_inputs)
    assert all(op in {"outline", "extract", "chapters", "overview"} for op, _ in model.inputs)


@pytest.mark.parametrize("kind", ["speech", "frame"])
def test_segments_publish_sequentially_share_body_flow_and_finish_with_one_overview(tmp_path, kind):
    evidence = [{**item, "kind": kind} for item in sources()]
    coordinator, model, snapshots, progress = flow(tmp_path, evidence)
    original = deepcopy(evidence)
    result = asyncio.run(coordinator.run())
    assert [op for op, _ in model.inputs] == ["outline", "chapters"] * 3 + ["overview"]
    assert [len(value["chapters"]) for value in snapshots] == [1, 2, 3]
    assert all(not value["complete"] for value in snapshots)
    assert [len(value["segment_guides"]) for value in snapshots] == [1, 2, 3]
    guides = result["segment_guides"]
    assert [guide["first_chapter_id"] for guide in guides] == ["C1", "C2", "C3"]
    assert [(g["start_seconds"], g["end_seconds"]) for g in guides] == [(0, 1200), (1200, 2400), (2400, 3600)]
    assert all(g["summary"] == "视频介绍 TCP 慢启动与拥塞窗口增长。" for g in guides)
    for export in (notes_text, notes_markdown):
        text = export("长视频", 3600, result)
        assert text.index("第1段导览") < text.index("第2段导览") < text.index("第3段导览")
    assert [value["covered_until_seconds"] for value in snapshots] == [1200, 2400, 3600]
    assert [chapter["chapter_id"] for chapter in result["chapters"]] == ["C1", "C2", "C3"]
    assert [(c["start_seconds"], c["end_seconds"]) for c in result["chapters"]] == [(0, 1200), (1200, 2400), (2400, 3600)]
    assert [c["evidence_ids"] for c in result["chapters"]] == [["s0"], ["s1"], ["s2"]]
    assert all(c["timestamp_basis"] == kind for c in result["chapters"])
    for op, payload in model.inputs[2:-1]:
        assert "previous_context" not in payload
        if op == "outline":
            assert payload["context"] == []
            assert set(i["id"] for i in payload["anchors"]) <= set(i["id"] for i in payload["evidence"])
            assert coordinator.model.input_characters(op, payload) <= 15000
        else:
            assert payload["chapters"][0]["previous_chapter_outline"] == {
                "title": "慢启动", "core_question": "拥塞窗口如何增长？", "topics": ["拥塞窗口增长"]}
    assert set(model.inputs[-1][1]) == {"video_summaries", "content_type"}
    assert model.inputs[-1][1]["video_summaries"] == [guide["summary"] for guide in guides]
    assert result["complete"] and result["covered_until_seconds"] == 3600
    assert evidence == original and progress[-1]["completed"] == 3
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls
    assert len(list(tmp_path.glob("blocks-v4-segments-*.json"))) == 1


def test_short_video_does_not_duplicate_its_overview_as_segment_guide(tmp_path):
    coordinator, model, _, _ = flow(tmp_path, sources(1), duration=1200)
    result = asyncio.run(coordinator.run())
    assert result["segment_guides"] == []
    assert [op for op, _ in model.inputs] == ["outline", "chapters", "overview"]


def test_each_segment_passes_its_own_summary_to_writing_without_extra_calls(tmp_path):
    class Model(RecordingModel):
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.inputs.append((operation, deepcopy(payload)))
                self.calls += 1
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "主题", "core_question": "问题", "topics": ["要点"],
                    "start_anchor_id": payload["anchors"][0]["id"]}],
                    "video_summary": "片段主题" + payload["evidence"][0]["id"]})
            return await super().generate(operation, payload, validate)
    coordinator, model, _, _ = flow(tmp_path, model_type=Model)
    result = asyncio.run(coordinator.run())
    assert [op for op, _ in model.inputs] == ["outline", "chapters"] * 3 + ["overview"]
    expected = ["片段主题E1", "片段主题E2", "片段主题E3"]
    assert [payload["video_summary"] for op, payload in model.inputs if op == "chapters"] == expected
    assert [guide["summary"] for guide in result["segment_guides"]] == expected
    calls = model.calls
    assert asyncio.run(coordinator.run()) == result and model.calls == calls


def test_short_outline_preview_is_published_before_body_and_restored_from_cache(tmp_path):
    previews = []
    class Model(RecordingModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                assert previews == ["视频介绍 TCP 慢启动与拥塞窗口增长。"]
                assert list(tmp_path.glob("blocks-v4-outline-*.json"))
            return await super().generate(operation, payload, validate)
    coordinator, model, _, _ = flow(tmp_path, sources(1), Model, duration=1200,
                                    outline_ready=previews.append)
    asyncio.run(coordinator.run())
    assert previews == ["视频介绍 TCP 慢启动与拥塞窗口增长。"]
    calls = model.calls
    previews.clear()
    asyncio.run(coordinator.run())
    assert previews == ["视频介绍 TCP 慢启动与拥塞窗口增长。"]
    assert model.calls == calls == 3


def test_segmented_video_does_not_publish_whole_video_outline_preview(tmp_path):
    previews = []
    coordinator, _, _, _ = flow(tmp_path, outline_ready=previews.append)
    asyncio.run(coordinator.run())
    assert previews == []


@pytest.mark.parametrize("stop", [False, True])
def test_pipeline_outline_preview_survives_body_failure_or_stop_and_resumes(client, app, video_file, stop):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    starts = []
    class Model(FakeModel):
        fail = True
        async def generate(self, operation, payload, validate):
            starts.append(operation)
            if operation == "chapters":
                task = client.get(f"/api/v1/tasks/{identifier}").json()
                assert task["config"]["outline_preview"] == "视频介绍 TCP 慢启动与拥塞窗口增长。"
                assert task["notes_state"] == "none" and "result" not in task
                assert client.get(f"/api/v1/analyses/{identifier}").status_code == 409
                if self.fail:
                    if stop:
                        assert client.post(f"/api/v1/tasks/{identifier}/stop").status_code == 200
                        raise TaskPaused()
                    raise AppError("model_network_error", "模拟正文失败")
            return await super().generate(operation, payload, validate)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, Model)
    app.state.worker.register("video", Task, pipeline)
    pipeline.run(identifier)
    task = client.get(f"/api/v1/tasks/{identifier}").json()
    assert task["status"] == ("paused" if stop else "failed")
    assert task["config"]["outline_preview"] == "视频介绍 TCP 慢启动与拥塞窗口增长。"
    Model.fail = False
    action = "resume" if stop else "retry"
    assert client.post(f"/api/v1/tasks/{identifier}/{action}").status_code == 202
    pipeline.run(identifier)
    assert client.get(f"/api/v1/tasks/{identifier}").json()["status"] == "succeeded"
    assert starts.count("outline") == 1 and starts.count("overview") == 1
    created = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    assert "outline_preview" not in client.get(f"/api/v1/tasks/{created}").json()["config"]


def test_title_is_background_and_overview_uses_only_planning_video_summary(tmp_path):
    class Model(RecordingModel):
        async def generate(self, operation, payload, validate):
            value = await super().generate(operation, payload, validate)
            if operation == "chapters":
                for chapter in value["chapters"]:
                    chapter["summary"] = "正文事实与案例不应进入导览请求"
            return value
    coordinator, model, _, _ = flow(tmp_path, model_type=Model, video_title="坦克算法对抗赛")
    asyncio.run(coordinator.run())
    assert all(payload["video_title"] == "坦克算法对抗赛" for op, payload in model.inputs)
    overview = model.inputs[-1][1]
    assert "正文事实与案例不应进入导览请求" not in json.dumps(overview, ensure_ascii=False)
    assert "拥塞窗口如何增长" not in json.dumps(overview, ensure_ascii=False)
    assert overview["video_summaries"] == ["视频介绍 TCP 慢启动与拥塞窗口增长。"] * 3


def test_overview_failure_retries_only_overview_and_keeps_exportable_body(tmp_path):
    class Model(RecordingModel):
        fail = True
        async def generate(self, operation, payload, validate):
            if operation == "overview" and self.fail:
                raise AppError("model_network_error", "模拟概览失败")
            return await super().generate(operation, payload, validate)
    coordinator, model, snapshots, _ = flow(tmp_path, model_type=Model)
    with pytest.raises(AppError):
        asyncio.run(coordinator.run())
    prefix = snapshots[-1]
    for export in (notes_text, notes_markdown):
        assert "未完成笔记" in export("长视频", 3600, prefix)
        assert "3/3" in export("长视频", 3600, prefix)
    calls = model.calls
    model.fail = False
    result = asyncio.run(coordinator.run())
    assert model.calls == calls + 1 and result["chapters"] == prefix["chapters"]
    assert result["segment_guides"] == prefix["segment_guides"]
    assert "未完成笔记" not in notes_text("长视频", 3600, result)


def test_failed_segment_keeps_prefix_and_reuses_completed_chapter_checkpoints(tmp_path):
    class Model(RecordingModel):
        fail = True
        def input_characters(self, operation, payload):
            if operation == "chapters" and len(payload["chapters"]) > 1:
                return 1000000
            return super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.inputs.append((operation, deepcopy(payload)))
                self.calls += 1
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": i["id"], "core_question": "问题", "topics": ["新增内容"],
                    "start_anchor_id": i["id"]} for i in payload["anchors"]], "video_summary": "片段概要"})
            if operation == "chapters" and payload["chapters"][0]["chapter_id"] == "C3" and self.fail:
                await asyncio.sleep(0)
                raise AppError("invalid_model_output", "模拟章节失败")
            return await super().generate(operation, payload, validate)
    evidence = sources(2) + [{"id": "s2", "kind": "speech", "start_seconds": 1300, "end_seconds": 1310, "text": "第三章"}]
    coordinator, model, snapshots, _ = flow(tmp_path, evidence, Model, duration=2400)
    with pytest.raises(AppError):
        asyncio.run(coordinator.run())
    assert len(snapshots) == 1 and snapshots[0]["covered_until_seconds"] == 1200
    completed = [p for op, p in model.inputs if op == "chapters" and p["chapters"][0]["chapter_id"] == "C2"]
    assert len(completed) == 1
    model.fail = False
    result = asyncio.run(coordinator.run())
    assert len(result["chapters"]) == 3
    assert len([p for op, p in model.inputs if op == "chapters" and p["chapters"][0]["chapter_id"] == "C2"]) == 1


def test_four_inflight_requests_cancel_within_two_seconds_and_resume_saved_prefix(tmp_path):
    started, count, cancelled = asyncio.Event(), 0, []
    class Model(RecordingModel):
        slow = True
        def input_characters(self, operation, payload):
            if operation == "chapters" and len(payload["chapters"]) > 1:
                return 1000000
            return super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            nonlocal count
            if operation == "outline":
                self.inputs.append((operation, deepcopy(payload)))
                self.calls += 1
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": i["id"], "core_question": "问题", "topics": ["内容"],
                    "start_anchor_id": i["id"]} for i in payload["anchors"]], "video_summary": "主题"})
            if operation == "chapters" and payload["chapters"][0]["chapter_id"] != "C1" and self.slow:
                count += 1
                if count == 4:
                    started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.append(payload["chapters"][0]["chapter_id"])
                    raise
            return await super().generate(operation, payload, validate)
    evidence = sources(1) + [{"id": f"s{i + 1}", "kind": "speech", "start_seconds": 1200 + i * 10,
                              "end_seconds": 1201 + i * 10, "text": "后续内容"} for i in range(4)]
    coordinator, model, snapshots, _ = flow(tmp_path, evidence, Model, duration=2400)
    async def run():
        task = asyncio.create_task(coordinator.run())
        await asyncio.wait_for(started.wait(), 2)
        coordinator.stop.set()
        with pytest.raises(TaskPaused):
            await asyncio.wait_for(task, 2)
        assert len(cancelled) == 4 and len(snapshots) == 1
        calls = model.calls
        coordinator.stop.clear()
        model.slow = False
        result = await coordinator.run()
        assert len(result["chapters"]) == 5 and model.calls == calls + 5
    asyncio.run(run())


@pytest.mark.parametrize("status", ["running", "paused", "failed", "interrupted"])
def test_partial_notes_api_status_and_downloads(client, app, video_file, status):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    result = {"format_version": 4, "model": "fake", "prompt_version": "test", "coverage_note": "", "basis": ["speech"], "title": "部分笔记", "tags": [], "overview_paragraphs": [],
        "chapters": [{"chapter_id": "C1", "title": "第一章", "markdown": "已生成正文", "evidence_ids": ["partial-source"],
                      "start_seconds": 0, "end_seconds": 1}], "source_issues": [], "evidence_ids": ["partial-source"],
        "complete": False, "covered_until_seconds": 1, "completed_segments": 1, "total_segments": 2, "notes_revision": 1,
        "segment_guides": [{"index": 1, "start_seconds": 0, "end_seconds": 1,
                            "first_chapter_id": "C1", "summary": "第1段的导览说明"}]}
    with app.state.sessions.begin() as session:
        session.add(Evidence(id="partial-source", video_id=video["id"], kind="speech", start_seconds=0,
                             end_seconds=1, text="原文", image_name=None, confidence=None))
        task = session.get(Task, identifier)
        task.status, task.stage, task.result = status, "summarizing", result
    public = client.get(f"/api/v1/tasks/{identifier}").json()
    assert public["notes_state"] == "partial" and public["notes_revision"] == 1 and "result" not in public
    response = client.get(f"/api/v1/analyses/{identifier}")
    assert response.status_code == 200 and response.json()["complete"] is False
    assert response.json()["segment_guides"] == result["segment_guides"]
    for format in ("txt", "md"):
        exported = client.get(f"/api/v1/analyses/{identifier}/notes?format={format}")
        assert exported.status_code == 200 and "未完成笔记" in exported.text and "已生成正文" in exported.text
        assert exported.text.index("第1段的导览说明") < exported.text.index("已生成正文")


def test_segment_progress_is_monotonic_across_segments_and_restore():
    values = []
    for completed, phase, stage in [(0, "planning", "segments"), (0, "summarizing", "segments"),
            (0, "issues", "segments"), (1, "completed", "segments"), (1, "planning", "segments"),
            (1, "summarizing", "segments"), (1, "summarizing", "transcribing"), (2, "overview", "finalizing")]:
        segment = {"completed": completed, "total": 2, "phase": phase, "chapter_processed": 1, "chapter_total": 1}
        task = SimpleNamespace(status="running", stage=stage, config={"segmented": True, "segment_progress": segment},
                               result={"format_version": 4, "chapters": [{}]})
        values.append(task_progress(task)["percent"])
    assert values == sorted(values) and values[-1] < 100


@pytest.mark.parametrize("legacy_prefix", [False, True])
def test_pipeline_publishes_prefix_before_next_segment_and_retry_freezes_settings(client, app, video_file, legacy_prefix):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        row = session.get(Video, video["id"])
        row.duration, row.audio_codec = 2400, "aac"
        task = session.get(Task, identifier)
        task.config = {**{k: v for k, v in task.config.items() if k != "frame_count"}, "frame_interval": 1200}
    settings = replace(app.state.pipeline.get_settings(), frame_interval=1200)
    config = extraction_config(settings)
    config.pop("frame_count")
    signature = extraction_signature(config)
    folder = settings.data_dir / "videos" / video["id"]
    (folder / "audio.wav").write_bytes(b"isolated recognizer input")
    for index in range(2):
        (folder / f"ocr-{signature}-{index:05d}.json").write_text('{"text":""}', encoding="utf-8")
    starts, models, transcriptions = [], [], []
    class Recognizer(FakeRecognizer):
        def transcribe(self, path):
            transcriptions.append(path)
            return sources(2)
    class Model(FakeModel):
        fail = True
        def __init__(self, config):
            super().__init__(config)
            models.append((config.llm_model, config.summary_prompt, config.llm_section_concurrency,
                           config.llm_request_interval_seconds))
        async def generate(self, operation, payload, validate):
            starts.append((operation, [c["chapter_id"] for c in payload.get("chapters", [])]))
            if operation == "outline" and payload["anchors"][0]["start_seconds"] == 1200:
                public = client.get(f"/api/v1/tasks/{identifier}").json()
                assert public["notes_state"] == "partial" and "result" not in public
                assert public["notes_revision"] == 1
                assert public["segment_progress"]["completed"] == 1
                note = client.get(f"/api/v1/analyses/{identifier}").json()
                assert len(note["chapters"]) == 1 and note["covered_until_seconds"] == 1200
                assert note["segment_guides"][0]["first_chapter_id"] == "C1"
            if operation == "chapters" and payload["chapters"][0]["chapter_id"] == "C2" and self.fail:
                raise AppError("model_network_error", "模拟第二段正文失败")
            return await super().generate(operation, payload, validate)
    pipeline = Pipeline(lambda snapshot=settings: snapshot, app.state.sessions, app.state.diagnostics, Recognizer, Model)
    app.state.worker.register("video", Task, pipeline)
    original = (settings.llm_model, settings.summary_prompt, settings.llm_section_concurrency,
                settings.llm_request_interval_seconds)
    pipeline.run(identifier)
    assert client.get(f"/api/v1/tasks/{identifier}").json()["status"] == "failed"
    assert "1/2" in client.get(f"/api/v1/analyses/{identifier}/notes").text
    if legacy_prefix:
        with app.state.sessions.begin() as session:
            task = session.get(Task, identifier)
            task.result = {key: value for key, value in task.result.items() if key != "segment_guides"}
    pipeline.get_settings = lambda snapshot=replace(settings, llm_model="changed/model", summary_prompt="changed",
                                llm_section_concurrency=1, llm_request_interval_seconds=10): snapshot
    percentages = [client.get(f"/api/v1/tasks/{identifier}").json()["progress"]["percent"]]
    update = pipeline.update
    def observe(*args, **kwargs):
        update(*args, **kwargs)
        percentages.append(client.get(f"/api/v1/tasks/{identifier}").json()["progress"]["percent"])
    pipeline.update = observe
    Model.fail = False
    assert client.post(f"/api/v1/tasks/{identifier}/retry").status_code == 202
    pipeline.run(identifier)
    task = client.get(f"/api/v1/tasks/{identifier}").json()
    assert task["status"] == "succeeded" and task["notes_revision"] == (4 if legacy_prefix else 3)
    assert task["progress"]["percent"] == 100 and task["notes_state"] == "ready"
    assert starts.count(("chapters", ["C1"])) == 1
    assert starts.count(("overview", [])) == 1
    assert starts.count(("outline", [])) == 2
    assert models == [original, original] and len(transcriptions) == 1
    assert percentages == sorted(percentages)
    assert "未完成笔记" not in client.get(f"/api/v1/analyses/{identifier}/notes").text


def test_completed_files_recover_after_snapshot_publication_failure(tmp_path):
    coordinator, model, snapshots, _ = flow(tmp_path)
    def fail(value):
        raise OSError("模拟发布中断")
    coordinator.publish = fail
    with pytest.raises(OSError):
        asyncio.run(coordinator.run())
    calls = model.calls
    coordinator.publish = lambda value: snapshots.append(deepcopy(value))
    assert asyncio.run(coordinator.run())["complete"]
    assert model.calls == calls + 5 and len(snapshots) == 3


def test_corrupt_completed_segment_is_rejected_before_new_model_calls(tmp_path):
    coordinator, model, _, _ = flow(tmp_path)
    asyncio.run(coordinator.run())
    cache = next(tmp_path.glob("blocks-v4-segment-*.json"))
    record = json.loads(cache.read_text(encoding="utf-8"))
    record["drafts"][0]["markdown"] = ""
    cache.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    calls = model.calls
    with pytest.raises(ValueError):
        asyncio.run(coordinator.run())
    assert model.calls == calls


def test_overview_payload_preserves_all_segment_summaries_without_truncation(tmp_path):
    coordinator, model, _, _ = flow(tmp_path)
    record = {"outline": {"video_summary": "片段主题。" * 1000,
                         "chapters": [{"title": "重要主题" * 200, "core_question": "核心问题", "topics": ["重要概念" * 1000]}]},
              "chapters": [{"chapter_id": "C1", "title": "重要主题" * 200}],
              "drafts": [{"summary": "核心概念与条件。" * 1000}]}
    payload = coordinator.overview_payload([record] * 25)
    assert not coordinator.fits("overview", payload)
    assert payload == {"video_summaries": [record["outline"]["video_summary"]] * 25}
    assert model.calls == 0
