import asyncio
from copy import deepcopy
import json

import pytest

from app.analysis.budgets import message_characters
from app.analysis.concise_prompt import concise_messages
from app.analysis.content_types import CONTENT_TYPES
from app.config import Settings
from app.documents.detailed import DetailedDocumentGeneration
from app.errors import DocumentStopped
from app.documents.material import detailed_segments, preceding
from app.documents.tasks import preview
from app.documents.model import DocumentModelClient
from app.documents.tasks import task_config
from app.errors import AppError
from app.videos.detailed import DetailedVideoGeneration
from app.videos.model import ModelClient
from .fakes import FakeModel


def test_document_concise_compacts_only_sending_copy_and_preserves_formula_rules():
    payload = {"document_title": "表格", "primary": [{"id": "D1", "text": "=B2*2", "label": "工作表第1行",
        "start_offset": 0, "end_offset": 5}], "context": [{"id": "D1", "text": "前文", "label": "第1行",
        "start_offset": 0, "end_offset": 2}], "content_type": CONTENT_TYPES["课程或教材"]}
    original = deepcopy(payload)
    model = DocumentModelClient(Settings())
    messages = model.messages("concise", payload)
    sent = json.loads(messages[-1]["content"])
    assert sent == {**payload, "primary": [{"id": "D1", "text": "=B2*2"}], "context": [{"id": "D1", "text": "前文"}]}
    assert "不自行计算表格公式" in messages[0]["content"] and "不推算统计结论" in messages[0]["content"]
    assert "每节是一个具体标题和一段一至两句话的内容介绍" in messages[0]["content"]
    assert "只概括原公式和表格的主题或用途" in messages[0]["content"]
    assert "不展开公式、解题步骤、计算答案或完整代码" in messages[0]["content"]
    assert model.input_characters("concise", payload) == message_characters(messages)
    assert payload == original
    video_payload = {"primary": [{"id": "E1", "kind": "speech", "text": "讲解"}], "context": []}
    from app.videos.prompts import CONCISE_VIDEO_RULES, CONCISE_VIDEO_INSTRUCTION
    assert ModelClient(Settings()).messages("concise", video_payload) == concise_messages(video_payload, Settings().summary_prompt, CONCISE_VIDEO_RULES, instruction=CONCISE_VIDEO_INSTRUCTION)


@pytest.mark.parametrize("outline", [{}, {"title": "标题", "core_question": "问题", "topics": ["要点"]},
    {"title": '"\\\n' * 1000, "core_question": "概念" * 2000, "topics": ["条件" * 1000] * 4},
    {"title": "标题", "core_question": "问题", "topics": ["要点" * 100] * 20}])
def test_previous_outline_copy_has_bounded_serialized_size(outline):
    from app.documents.material import compact_outline
    original = deepcopy(outline)
    value = compact_outline(outline)
    assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) <= 1000
    assert outline == original
    assert set(value) == set(outline)
    if len(json.dumps(outline, ensure_ascii=False, separators=(",", ":"))) <= 1000:
        assert value == outline


def large_material():
    return {"version": 2, "units": [{"id": "D1", "text": "knowledge and explanation.\n" * 3000,
        "label": "正文1", "format": "text", "locator": {"block": 1}}]}


def test_detailed_document_preflight_reserves_previous_outline_in_actual_messages(tmp_path):
    model = DocumentModelClient(Settings(llm_input_text_limit=25000))
    material = large_material()
    info = preview(material, model.settings, "course")
    fragments, segments = detailed_segments(material, model, "course")
    assert info["eligible"] and info["estimated_segments"] == len(segments) > 1
    gen = DetailedDocumentGeneration("budget", tmp_path, task_config(model.settings, "course", info), material,
        model, asyncio.Event(), lambda: None, lambda *args: None, lambda _: None)
    previous = {"title": "basic loop concepts", "core_question": "Why do conditions determine termination? " * 10,
        "topics": ["Explain iteration and boundary cases. " * 10] * 5}
    for segment in segments:
        payload = {"document_title": "course", "anchors": [i["id"] for i in segment],
            "evidence": [{"id": i["id"], "text": i["text"]} for i in segment],
            "context": [{"id": i["id"], "text": i["text"]} for i in preceding(fragments, fragments.index(segment[0]))],
            "previous_chapter_outline": previous, "content_type": CONTENT_TYPES["课程或教材"]}
        assert model.input_characters("outline", gen.input_payload(payload)) <= 25000
    chapter = {"chapter_id": "C1", "primary": [], "previous_chapter_outline": previous}
    for operation, payload in [("chapters", {"chapters": [chapter]}), ("extract", {"chapter": chapter})]:
        sent = json.loads(model.messages(operation, gen.input_payload(payload))[-1]["content"])
        copy = sent["chapters"][0] if operation == "chapters" else sent["chapter"]
        assert len(json.dumps(copy["previous_chapter_outline"], ensure_ascii=False, separators=(",", ":"))) <= 1000


def test_document_detailed_long_outline_pause_resume_keeps_boundaries(tmp_path):
    class Model(DocumentModelClient):
        def __init__(self, settings):
            super().__init__(settings)
            self.inputs = []

        async def generate(self, operation, payload, validate):
            assert self.input_characters(operation, payload) <= self.settings.llm_input_text_limit
            self.inputs.append((operation, deepcopy(payload)))
            if operation == "outline":
                value = {"content_type": payload.get("content_type", CONTENT_TYPES["课程或教材"]),
                    "document_summary": "材料导览", "chapters": [{"title": "循环结构",
                    "core_question": "条件和更新如何决定循环终止？" * 120, "topics": ["边界和终止条件" * 100],
                    "start_anchor_id": payload["anchors"][0]}]}
            elif operation == "chapters":
                value = {"chapters": [{"chapter_id": c["chapter_id"], "summary": "必要条件", "markdown": "正文解释。", "issues": []}
                                      for c in payload["chapters"]]}
            elif operation == "extract":
                value = {"facts": [{"text": "原文中的必要条件。"}], "issues": []}
            else:
                value = {"title": "概览", "tags": [], "overview_paragraphs": ["材料说明循环结构。"]}
            return validate(value)

    material = large_material()
    model = Model(Settings(llm_input_text_limit=25000))
    info = preview(material, model.settings, "course")
    config = task_config(model.settings, "course", info)
    stop, drafts = asyncio.Event(), []
    def publish(value):
        drafts.append(deepcopy(value))
        stop.set()
    def generation(callback):
        return DetailedDocumentGeneration("document", tmp_path, config, material, model, stop, lambda: None,
            lambda *args: None, callback)
    # Detailed checks are supplied by the pipeline; simulate its stop check here.
    gen = generation(publish)
    def check():
        if stop.is_set():
            raise DocumentStopped()
    gen.check = check
    with pytest.raises(DocumentStopped):
        asyncio.run(gen.run())
    first = drafts[0]
    stop.clear()
    resumed = generation(drafts.append)
    result = asyncio.run(resumed.run())
    assert result["complete"] and result["total_segments"] == info["estimated_segments"]
    assert result["chapters"][0] == first["chapters"][0]
    assert len([p for op, p in model.inputs if op == "outline"]) == result["total_segments"]
    spans = [s for c in result["chapters"] for s in c["source_spans"]]
    assert spans[0]["start_offset"] == 0 and spans[-1]["end_offset"] == len(material["units"][0]["text"])
    assert all(a["end_offset"] == b["start_offset"] for a, b in zip(spans, spans[1:]))


class OverviewModel(FakeModel):
    def __init__(self, settings):
        super().__init__(settings)
        self.inputs = []
        self.fail_final = False
        self.expand = False

    async def generate(self, operation, payload, validate):
        assert self.input_characters(operation, payload) <= self.settings.llm_input_text_limit
        self.inputs.append(deepcopy(payload))
        self.calls += 1
        if self.fail_final and all(s.startswith("reduced-") for s in payload["video_summaries"]):
            raise AppError("model_network_error", "模拟最终概览失败")
        return validate({"title": "概览", "tags": [], "overview_paragraphs": ["扩大" * 10000 if self.expand else f"reduced-{self.calls}"]})


def video_generation(tmp_path, limit=4500):
    model = OverviewModel(Settings(llm_input_text_limit=limit))
    gen = DetailedVideoGeneration("overview", model, model.settings, tmp_path, [], 10,
        asyncio.Event(), lambda: None, lambda _: None, {}, video_title="材料标题")
    gen.content_type = CONTENT_TYPES["课程或教材"]
    return gen, model


def records(summaries):
    return [{"outline": {"video_summary": summary, "chapters": [{"title": "EXCLUDE_TITLE",
        "core_question": "EXCLUDE_QUESTION", "topics": ["EXCLUDE_TOPICS"]}]},
        "chapters": [{"chapter_id": f"C{i + 1}", "markdown": "EXCLUDE_BODY"}],
        "drafts": [{"summary": "EXCLUDE_DRAFT_SUMMARY"}]} for i, summary in enumerate(summaries)]


@pytest.mark.parametrize("summaries", [["片段导览1"], ["片段导览1", "片段导览2", "片段导览3"]])
def test_video_overview_uses_only_ordered_planning_summaries(tmp_path, summaries):
    gen, model = video_generation(tmp_path)
    result = asyncio.run(gen.overview(records(summaries)))
    assert result["overview_paragraphs"] == ["reduced-1"] and model.calls == 1
    assert model.inputs == [{"video_summaries": summaries, "video_title": "材料标题", "content_type": CONTENT_TYPES["课程或教材"]}]


def test_video_overview_splits_without_losing_text_and_retries_only_missing_final(tmp_path):
    gen, model = video_generation(tmp_path)
    summaries = ["".join(f"第一段第{i}项解释。\n" for i in range(1400)) + "TAIL_MARKER",
                 "".join(f"第二段第{i}项导览。" for i in range(600))]
    model.fail_final = True
    with pytest.raises(AppError, match="模拟最终概览失败"):
        asyncio.run(gen.overview(records(summaries)))
    raw = [p for p in model.inputs if not all(s.startswith("reduced-") for s in p["video_summaries"])]
    assert "".join(s for p in raw for s in p["video_summaries"]) == "".join(summaries)
    count = model.calls
    model.fail_final = False
    result = asyncio.run(gen.overview(records(summaries)))
    assert model.calls == count + 1
    assert asyncio.run(gen.overview(records(summaries))) == result and model.calls == count + 1
    assert "EXCLUDE" not in json.dumps(model.inputs)


def test_video_overview_nonconvergence_keeps_successful_checkpoints(tmp_path):
    gen, model = video_generation(tmp_path)
    model.expand = True
    with pytest.raises(AppError) as error:
        asyncio.run(gen.overview(records(["主题导览。" * 2000])))
    assert error.value.code == "model_input_budget"
    assert list(tmp_path.glob("*overview*.json")) and model.calls > 0


def test_video_overview_fixed_prompt_over_budget_sends_nothing(tmp_path):
    gen, model = video_generation(tmp_path, limit=1)
    with pytest.raises(AppError) as error:
        asyncio.run(gen.overview(records(["导览"])))
    assert error.value.code == "model_input_budget" and model.calls == 0
