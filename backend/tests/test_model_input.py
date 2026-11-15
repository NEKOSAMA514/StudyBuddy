from .fakes import model_validator
from app.analysis.budgets import message_characters
from app.analysis.content_types import CONTENT_TYPES
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.videos.detailed import DetailedVideoGeneration
from app.videos.model import ModelClient
from app.videos.model_input import deduplicate_ocr, pack_chapter_material
from app.videos.concise import payload_for
from app.config import Settings
from app.errors import AppError
from .fakes import run_model, model_content


def materials():
    common = "终点前：Ag+与SCN−形成白色沉淀，操作时应保持规定的酸度。\r\n"
    return [{"id": f"E{i + 1}", "kind": "frame", "start_seconds": i, "end_seconds": i,
             "text": common * 4 + f"AgCl / AgC1；1-{i + 1}mL；Fe3+；0；第{i + 1}次。\n"}
            for i in range(4)]


@pytest.mark.parametrize("operation", ["outline", "chapters", "extract"])
def test_deduplication_preserves_speech_differences_and_independent_chapter_material(operation):
    evidence = materials()
    speech = {"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 0,
              "text": evidence[0]["text"]}
    chapter = {"chapter_id": "C1", "primary": [evidence[0], speech, *evidence[1:3]], "context": evidence[3:]}
    if operation == "chapters":
        payload = {"chapters": [chapter, {**chapter, "chapter_id": "C2"}]}
    elif operation == "extract":
        payload = {"chapter": chapter}
    else:
        payload = {"anchors": [{"id": "E1", "start_seconds": 0}, {"id": "S1", "start_seconds": 0}],
                   "evidence": [*evidence, speech]}
    before = deepcopy(payload)
    compact = deduplicate_ocr(payload)
    if operation == "outline":
        records = compact["evidence"]
        assert compact["anchors"] == [{"id": "S1", "start_seconds": 0}]
    else:
        chapters = compact["chapters"] if operation == "chapters" else [compact["chapter"]]
        for chapter in chapters:
            assert [item["id"] for item in chapter["primary"]] == ["S1", "E2", "E3"]
        records = chapters[0]["primary"] + chapters[0]["context"]
    assert next(item for item in records if item["id"] == "S1") == speech
    assert {item["id"]: item["text"] for item in records if item["kind"] == "frame"} == {
        "E2": evidence[1]["text"].rstrip(),
        "E3": evidence[2]["text"].splitlines()[-1],
        "E4": evidence[3]["text"].rstrip() if operation != "outline" else evidence[3]["text"].splitlines()[-1],
    }
    assert payload == before and deduplicate_ocr(payload) == compact
    assert len(json.dumps(compact, ensure_ascii=False).encode()) < len(json.dumps(payload, ensure_ascii=False).encode())


def test_identical_frames_are_removed_but_repeated_matrix_rows_in_one_frame_remain():
    text = "矩阵计算过程如下\n1 0 0\n1 0 0\n0 1 0"
    evidence = [{"id": f"E{i}", "kind": "frame", "text": text, "start_seconds": i, "end_seconds": i}
                for i in range(2)]
    assert deduplicate_ocr({"evidence": evidence}) == {"evidence": evidence[:1]}
    unique = [evidence[0], {**evidence[1], "text": "矩阵计算的新步骤\n1 0 0\n0 0 1"}]
    assert deduplicate_ocr({"evidence": unique}) == {"evidence": unique}


def test_context_speech_cannot_remove_primary_ocr():
    frame = {"id": "E1", "kind": "frame", "text": "这里是完整的主要画面材料", "start_seconds": 0, "end_seconds": 0}
    payload = {"primary": [frame], "context": [{**frame, "id": "S1", "kind": "speech"}]}
    assert deduplicate_ocr(payload) == payload


def subtitle_materials():
    subtitle = "每轮结束之后需要重新检查所有坦克的位置以及基地剩余的生命值" * 8
    return [
        {"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 10,
         "text": "现在说明游戏规则：" + subtitle + "。注意 Fe2+ 与 0.1mL。"},
        {"id": "F1", "kind": "frame", "start_seconds": 5, "end_seconds": 5,
         "text": "积分表：A 14；B 12\n" + subtitle + "\n注意 Fe3+ 与 0.01mL。AgCl / AgC1。"},
    ]


@pytest.mark.parametrize("operation", ["extract", "concise"])
def test_reappearing_screen_label_is_sent_with_nearby_speech(operation):
    def item(identifier, kind, start, end, text):
        return {"id": identifier, "kind": kind, "start_seconds": start, "end_seconds": end, "text": text}

    evidence = [
        item("E1", "speech", 0, 2, "开场提到了 DeepSeek"),
        item("E2", "frame", 20, 20, "DeepSeek"),
        item("E3", "frame", 20.4, 20.4, "DeepSeek"),
        item("E4", "frame", 125.567, 125.567, "DeepSeek"),
        item("E5", "speech", 126, 128.04, "我那不是打个比方嘛"),
    ]
    chapter = {"primary": evidence, "context": []}
    payload = {"chapter": chapter} if operation == "extract" else payload_for("视频", evidence, [])
    model = ModelClient(Settings())
    messages = model.messages(operation, payload)
    sent = json.loads(messages[-1]["content"])
    records = sent.get("chapter", sent)["primary"]

    assert [row["id"] for row in records] == ["E1", "E2", "E4", "E5"]
    assert records[1] == {"id": "E2", "kind": "frame", "text": "DeepSeek"}
    assert records[2] == {"id": "E4", "kind": "frame", "text": "DeepSeek", "nearby_speech_id": "E5"}
    assert records[3]["text"] == evidence[-1]["text"]
    assert model.input_characters(operation, payload) == message_characters(messages)


def test_screen_text_reappearing_after_a_different_frame_is_kept():
    evidence = [{"id": f"E{i}", "kind": "frame", "text": text, "start_seconds": i * 0.5,
                 "end_seconds": i * 0.5}
                for i, text in enumerate(("DeepSeek", "Claude", "DeepSeek"))]
    assert deduplicate_ocr({"evidence": evidence}) == {"evidence": evidence}


def test_repeated_slide_body_is_compacted_while_labels_and_new_values_are_kept():
    repeated = "本节介绍不同情况下如何选择合适的实验条件。\n这里整理了实验之前需要确认的所有准备工作。"
    first = {"id": "E1", "kind": "frame", "start_seconds": 0, "end_seconds": 0,
             "text": f"DeepSeek\n{repeated}\n试剂用量是0.1毫升"}
    later = {**first, "id": "E2", "start_seconds": 10, "end_seconds": 10,
             "text": f"DeepSeek\n{repeated}\n试剂用量是0.01毫升"}
    assert deduplicate_ocr({"evidence": [first, later]})["evidence"] == [
        first, {**later, "text": "DeepSeek\n试剂用量是0.01毫升"}]


def test_ocr_filtered_as_speech_can_reappear_outside_that_speech():
    text = "这里介绍新版本与旧版本的主要差异"
    spoken = {"id": "S1", "kind": "speech", "text": text, "start_seconds": 0, "end_seconds": 2}
    frame = {"id": "E1", "kind": "frame", "text": text, "start_seconds": 1.5, "end_seconds": 1.5}
    later = {**frame, "id": "E2", "start_seconds": 2.5, "end_seconds": 2.5}
    assert deduplicate_ocr({"evidence": [spoken, frame, later]}) == {"evidence": [spoken, later]}


def test_short_screen_label_is_kept_when_the_same_name_is_spoken():
    evidence = [
        {"id": "S1", "kind": "speech", "text": "这个问题刚才也问过 DeepSeek", "start_seconds": 0, "end_seconds": 2},
        {"id": "E1", "kind": "frame", "text": "DeepSeek", "start_seconds": 1, "end_seconds": 1},
    ]
    assert deduplicate_ocr({"evidence": evidence}) == {"evidence": evidence}


def test_nearby_speech_can_be_a_long_segment_overlapping_a_shorter_segment():
    evidence = [
        {"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 15, "text": "较长的语音"},
        {"id": "S2", "kind": "speech", "start_seconds": 3, "end_seconds": 4, "text": "较短的语音"},
        {"id": "E1", "kind": "frame", "start_seconds": 9, "end_seconds": 9, "text": "仅画面可见"},
    ]
    payload = {"chapter": {"primary": evidence[2:], "context": evidence[:2]}}
    sent = json.loads(ModelClient(Settings()).messages("extract", payload)[-1]["content"])
    assert sent["chapter"]["primary"][-1]["nearby_speech_id"] == "S1"


@pytest.mark.parametrize("operation", ["outline", "chapters", "extract"])
def test_duplicate_subtitle_is_removed_instead_of_encoded(operation):
    evidence = subtitle_materials()
    chapter = {"chapter_id": "C1", "primary": evidence, "context": []}
    payload = ({"evidence": evidence, "anchors": [{"id": "S1", "start_seconds": 0}]} if operation == "outline" else
               {"chapters": [chapter]} if operation == "chapters" else {"chapter": chapter})
    before = deepcopy(payload)
    sent = json.loads(ModelClient(Settings()).messages(operation, payload)[-1]["content"])
    assert "ocr_texts" not in sent
    records = sent["evidence"] if operation == "outline" else (
        sent["chapters"][0] if operation == "chapters" else sent["chapter"])["primary"]
    if operation == "chapters":
        frame, speech = records
        assert speech == {"id": "New_S1", "text": f'（F1出现处）{evidence[0]["text"]}（F1消失处）'}
        assert frame["id"] == "F1"
    else:
        speech, frame = records
        assert speech["text"] == evidence[0]["text"]
    assert frame["text"] == "积分表：A 14；B 12\n注意 Fe3+ 与 0.01mL。AgCl / AgC1。"
    assert payload == before


@pytest.mark.parametrize("operation", ["outline", "chapters", "extract"])
def test_sent_plain_text_matches_actual_request_budget(operation):
    evidence = subtitle_materials()
    chapter = {"chapter_id": "C1", "start_seconds": 0, "end_seconds": 10,
               "primary": evidence, "context": []}
    payload = ({"evidence": evidence, "anchors": [{"id": "S1", "start_seconds": 0}]} if operation == "outline" else
               {"chapters": [chapter]} if operation == "chapters" else {"chapter": chapter})
    before = deepcopy(payload)
    model = ModelClient(Settings())
    messages = model.messages(operation, payload)
    sent = json.loads(messages[-1]["content"])
    assert "ocr_texts" not in sent
    records = sent["evidence"] if operation == "outline" else (
        sent["chapters"][0] if operation == "chapters" else sent["chapter"])["primary"]
    assert all(isinstance(item["text"], str) for item in records)
    if operation == "chapters":
        assert [item["id"] for item in records] == ["F1", "New_S1"]
        assert message_characters(messages[-1:]) == sum(len(item["text"]) for item in records)
        assert records[-1]["text"].endswith("（F1消失处）")
    else:
        assert [(item["id"], item["text"]) for item in records] == [
            (item["id"], item["text"]) for item in deduplicate_ocr({"evidence": evidence})["evidence"]]
    assert "start_seconds" not in json.dumps(sent) and "end_seconds" not in json.dumps(sent)
    assert payload == before
    assert model.input_characters(operation, payload) == message_characters(messages)
    assert sum(len(item["text"]) for item in records) < sum(len(item["text"]) for item in evidence)


def test_similar_subtitles_are_not_deleted_and_speech_is_unchanged_without_ocr():
    evidence = subtitle_materials()
    assert deduplicate_ocr({"evidence": evidence[:1]}) == {"evidence": evidence[:1]}
    evidence[0]["text"] = "坦克每轮最多发射十四枚炮弹并减少基地十点生命值。"
    evidence[1]["text"] = "坦克每轮最多发射十五枚炮弹并减少基地十二点生命值。"
    before = deepcopy(evidence)
    assert deduplicate_ocr({"evidence": evidence}) == {"evidence": before}


def test_caption_punctuation_is_ignored_but_formula_and_decimal_differences_remain():
    speech = "第十回合地图中间刷出星星往后每二十回合刷一批。实验结果 Fe2+ 与 0.1mL。"
    evidence = [{"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 2, "text": speech},
                {"id": "E1", "kind": "frame", "start_seconds": 1, "end_seconds": 1,
                 "text": "第十回合，地图中间刷出星星，往后每二十回合刷一批\n实验结果 Fe3+ 与 0.01mL。"}]
    assert deduplicate_ocr({"evidence": evidence})["evidence"] == [
        evidence[0], {**evidence[1], "text": "实验结果 Fe3+ 与 0.01mL。"}]


@pytest.mark.parametrize("operation", ["chapters", "extract"])
def test_actual_requests_budget_and_repair_use_deduplicated_material(tmp_path, monkeypatch, operation):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    evidence = materials()
    coordinator = DetailedVideoGeneration("compressed", model, model.settings, tmp_path, evidence, 10,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    chapter = {"chapter_id": "C1", "primary": coordinator.evidence[:3], "context": coordinator.evidence[3:]}
    payload = {"chapters": [chapter]} if operation == "chapters" else {"chapter": chapter}
    before, path = deepcopy(payload), coordinator.path(operation, payload)
    good = ({"chapters": [{'chapter_id': 'C1', 'summary': '摘要', "markdown": '材料内容', "issues": []}]} if operation == "chapters" else
        {"facts": [{"text": '材料内容'}], "issues": []})
    outputs, requests = iter([{}, good]), []
    async def post(self, url, **kwargs):
        requests.append(deepcopy(kwargs["json"]["messages"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": model_content(next(outputs))}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    value = run_model(model, "generate", operation, payload, model_validator(operation, lambda output: coordinator.validate(operation, payload, output)))
    assert value == good and len(requests) == 2
    assert requests[1][:-(1 if operation == "chapters" else 2)] == requests[0]
    if operation != "chapters":
        assert requests[1][-2] == {"role": "assistant", "content": model_content({})}
    sent = json.loads(requests[0][-1]["content"])
    sent_chapter = sent["chapters"][0] if operation == "chapters" else sent["chapter"]
    if operation == "chapters":
        assert sent_chapter == {
            "chapter_id": "C1",
            "primary": [{"id": f"F{index + 1}", "text": row["text"].rstrip("\r\n")}
                        for index, row in enumerate(coordinator.evidence[:3])],
            "context": [{"id": "F4", "text": coordinator.evidence[3]["text"].rstrip("\r\n")}],
        }
    else:
        expected = deduplicate_ocr(chapter)
        assert sent_chapter == {**expected, **{
            key: [{field: item[field] for field in ("id", "kind", "text")} for item in expected[key]]
            for key in ("primary", "context")}}
    assert model.input_characters(operation, payload) == message_characters(requests[0])
    assert payload == before and coordinator.path(operation, payload) == path






def test_input_budget_uses_only_deduplicated_material(tmp_path, monkeypatch):
    model = ModelClient(Settings())
    coordinator = DetailedVideoGeneration("budget", model, model.settings, tmp_path, subtitle_materials(), 10,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    payload = {"video_summary": "当前片段主题", "chapters": [{"chapter_id": "C1", "primary": coordinator.evidence, "context": []}]}
    compressed_size = model.input_characters("chapters", payload)
    with monkeypatch.context() as patch:
        patch.setattr("app.videos.model_input._without_spoken_lines", lambda text, speech: text)
        full_size = model.input_characters("chapters", payload)
    assert compressed_size < full_size
    model.settings.llm_input_text_limit = compressed_size
    assert coordinator.fits("chapters", payload)
    model.settings.llm_input_text_limit -= 1
    assert not coordinator.fits("chapters", payload)


def timed_record(identifier, kind, start, end, text):
    return {"id": identifier, "kind": kind, "start_seconds": start, "end_seconds": end, "text": text}


def test_chapter_material_marks_original_fragments_and_keeps_standalone_frames_in_order():
    primary = [
        timed_record("E1", "speech", 0, 10, "  开场原话  "),
        timed_record("E2", "frame", 9, 65, "Claude"),
        timed_record("E3", "speech", 20, 25, "第一条说明"),
        timed_record("E4", "frame", 55, 55, "短时标牌"),
        timed_record("E5", "speech", 55, 60, "第一块末句"),
        timed_record("E6", "speech", 61, 65, "第二块原话"),
        timed_record("E7", "frame", 80, 90, "独立画面"),
        timed_record("E8", "speech", 100, 165, "超过一分钟的单条语音"),
        timed_record("E9", "speech", 160, 162, "仍从新块开始"),
        timed_record("E10", "frame", 161, 162, "Claude"),
    ]
    context = [timed_record("E11", "speech", 0, 10, "本章前文"), primary[1]]
    chapter = {"primary": primary, "context": context}
    before = deepcopy(chapter)
    sent, sources = pack_chapter_material(chapter)
    assert sent == {
        "primary": [
            {"id": "F1", "text": "Claude"},
            {"id": "F2", "text": "短时标牌"},
            {"id": "New_S1", "text": "（F1出现处）开场原话 第一条说明 （F2出现处）第一块末句（F2消失处）"},
            {"id": "New_S2", "text": "第二块原话（F1消失处）"},
            {"id": "F3", "text": "独立画面"},
            {"id": "F4", "text": "Claude"},
            {"id": "New_S3", "text": "（F4出现处）超过一分钟的单条语音"},
            {"id": "New_S4", "text": "仍从新块开始（F4消失处）"},
        ],
        "context": [{"id": "New_S5", "text": "本章前文"}],
    }
    assert [item["id"] for item in sources["New_S1"]] == ["E1", "E3", "E5"]
    assert sources["F1"] == [primary[1]] and sources["F4"] == [primary[-1]]
    assert chapter == before


def test_chapter_material_uses_nearest_fragments_on_either_side_of_a_speech_gap():
    records = [
        timed_record("E1", "speech", 10, 12, "停顿前的话"),
        timed_record("E2", "frame", 15, 15, "过渡画面"),
        timed_record("E3", "speech", 18, 20, "停顿后的话"),
    ]
    sent, _ = pack_chapter_material({"primary": records})
    assert sent["primary"] == [
        {"id": "F1", "text": "过渡画面"},
        {"id": "New_S1", "text": "（F1出现处）停顿前的话 停顿后的话（F1消失处）"},
    ]


def test_chapter_filter_uses_full_frame_range_and_preserves_visual_details():
    first, second = "这一段已经完整讲过", "后面的条件和限制"
    number = "实验要求体积精确保持在 0.01mL"
    frame_text = f"{first}，{second}\nClaude\n{number}\n{number.replace('0.01', '0.1')}\n条件变化时保持 Fe3+ 而非 Fe2+"
    primary = [
        timed_record("E1", "frame", 0, 10, frame_text),
        timed_record("E2", "speech", 2, 3, first),
        timed_record("E3", "speech", 3, 4, second),
        timed_record("E4", "speech", 4, 5, "Claude"),
        timed_record("E5", "speech", 5, 6, number),
        timed_record("E6", "frame", 8, 10, "仅在章前口播中出现的完整条件说明"),
    ]
    context = [timed_record("E7", "speech", 8, 9, primary[-1]["text"])]
    sent, _ = pack_chapter_material({"primary": primary, "context": context})
    assert sent["primary"][0] == {
        "id": "F1", "text": f"Claude\n{number.replace('0.01', '0.1')}\n条件变化时保持 Fe3+ 而非 Fe2+"}
    assert "（F1出现处）" in sent["primary"][1]["text"]
    assert sent["primary"][-1] == {"id": "F2", "text": primary[-1]["text"]}
    assert sent["context"] == [{"id": "New_S2", "text": context[0]["text"]}]


def test_chapter_aliases_resolve_before_partial_checkpoints_and_stay_stable_on_repair(tmp_path, monkeypatch):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    evidence = [
        timed_record("first", "speech", 0, 2, "甲先解释第一问"),
        timed_record("first-label", "frame", 1, 2, "甲的名牌"),
        timed_record("second", "speech", 4, 6, "甲再解释第二问"),
        timed_record("third", "speech", 100, 102, "乙先解释第三问"),
        timed_record("second-label", "frame", 101, 102, "乙的名牌"),
    ]
    coordinator = DetailedVideoGeneration("aliases", model, model.settings, tmp_path, evidence, 120,
                                          asyncio.Event(), lambda: None, lambda stage: None, {})
    chapters = [{"chapter_id": "C1", "primary": coordinator.evidence[:3], "context": []},
                {"chapter_id": "C2", "primary": coordinator.evidence[3:], "context": []}]
    payload = {"chapters": chapters}
    sent, snapshots = [], []

    def issue(alias, quote):
        return {"kind": "source_issue", "quote": quote, "evidence_ids": [alias],
                "reason": "等待核对", "critical": True}

    async def post(self, url, **kwargs):
        messages = kwargs["json"]["messages"]
        request = json.loads(next(message["content"] for message in reversed(messages)
                                  if message["role"] == "user" and message["content"].startswith("{")))
        if "repairs" in request:
            assert request["repairs"][0]["evidence_ids"] == ["E1", "E3"]
            value = {"repairs": [{"chapter_id": "C1", "block_index": 1, "quote": "甲再解释第二问"}]}
        else:
            sent.append(request["chapters"])
            first = len(sent) == 1
            if not first:
                snapshots.append(json.loads(coordinator.body_path(chapters[0]).read_text(encoding="utf-8")))
            issues = [issue("New_S1", "甲再解释第二问"), issue("New_S1", "甲先解释第一问 甲再解释第二问"),
                      issue("F1", "甲的名牌")] if first else [issue("F1", "乙的名牌")]
            value = {"chapters": [{"chapter_id": "C1" if first else "C2", "summary": "摘要",
                                   "markdown": "正文", "issues": issues}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": model_content(value)}}]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    async def run():
        async with model:
            result = await coordinator.pool([("chapters", payload)])
            assert await coordinator.pool([("chapters", payload)]) == result
            return await coordinator.source_issues(chapters, result[0]["chapters"])

    issues = asyncio.run(run())
    assert len(sent) == 2 and sent[1] == sent[0][1:]
    assert [[row["id"] for row in chapter["primary"]] for chapter in sent[0]] == [["F1", "New_S1"]] * 2
    assert [item["evidence_ids"] for item in snapshots[0]["issues"]] == [["E3"], ["E1", "E3"], ["E2"]]
    assert [item["evidence_ids"] for item in issues] == [["second"], ["first", "second"], ["first-label"], ["second-label"]]
    assert issues[1]["quote"] == "甲再解释第二问"


@pytest.mark.parametrize("operation", ["chapters", "extract"])
@pytest.mark.parametrize("extracted", [False, True])
def test_writing_budget_counts_all_transmitted_content(operation, extracted):
    chapter = {"chapter_id": "C" * 2000, "start_seconds": 100, "end_seconds": 200,
               "title": "本章", "core_question": "问题", "topics": ["主题甲", "主题乙"],
               "start_anchor_id": "E" * 2000,
               "previous_chapter_outline": {"title": "前章", "core_question": "前问", "topics": ["前提"]},
               "context": [{"id": "context", "kind": "speech", "text": "前一分钟原文"}]}
    chapter["facts" if extracted else "primary"] = [{"id": "source", "kind": "speech", "text": "本章原文"}]
    chapter["issues"] = [{"quote": "原文", "reason": "疑问", "evidence_ids": ["E" * 2000]}]
    payload = {"chapters": [chapter]} if operation == "chapters" else {"chapter": chapter}
    payload["video_title"] = "视频标题" * 1000
    payload["video_summary"] = "当前片段导览"
    model = ModelClient(Settings(summary_prompt="用户要求" * 10000))
    expected = message_characters(model.messages(operation, payload))
    assert model.input_characters(operation, payload) == expected
    assert len(json.dumps(model.messages(operation, payload), ensure_ascii=False).encode()) > 24000
    if operation == "chapters":
        payload["chapters"].append(deepcopy(chapter))
        assert model.input_characters(operation, payload) == message_characters(model.messages(operation, payload))
        assert model.input_characters(operation, payload) > expected


@pytest.mark.parametrize("operation", ["chapters", "extract"])
def test_writing_repair_uses_character_budget_at_exact_boundary(monkeypatch, operation):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    chapter = {"chapter_id": "C1", "title": "主题", "core_question": "问题", "topics": ["概念"],
               "primary": [{"id": "E1", "kind": "speech", "text": "中文原始材料" * 1000}], "context": []}
    payload = {"chapters": [chapter]} if operation == "chapters" else {"chapter": chapter}
    payload["video_summary"] = "当前片段主题导览"
    model.settings.llm_input_text_limit = model.input_characters(operation, payload)
    good = ({"chapters": [{"chapter_id": "C1", "summary": "摘要", "markdown": "正文", "issues": []}]} if operation == "chapters" else
            {"facts": [{"text": "内容"}], "issues": []})
    outputs, requests = iter([{}, good]), []
    async def request(messages, **kwargs):
        requests.append(deepcopy(messages))
        return model_content(next(outputs))
    monkeypatch.setattr(model, "request", request)
    with pytest.raises(AppError) as error:
        run_model(model, "generate", operation, payload, model_validator(operation, lambda value: value))
    assert error.value.code == "model_input_budget"
    assert len(requests) == 1
    assert json.loads(requests[0][-1]["content"])["video_summary"] == payload["video_summary"]



def test_outline_counts_complete_deduplicated_request_during_repair(tmp_path, monkeypatch):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    evidence = materials()
    evidence.append({"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 1,
                     "text": "对比 Fe2+、Fe3+，注意 0.1 与 0.01 的区别。"})
    payload = {"anchors": [{"id": "S1", "start_seconds": 0}], "evidence": evidence,
               "context": [evidence[0]]}
    before = deepcopy(payload)
    good = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "滴定", "core_question": "如何操作", "topics": ["反应与条件"],
                           "start_anchor_id": "S1"}], "video_summary": "介绍滴定。"}
    outputs, requests = iter([{}, good]), []
    async def post(self, url, **kwargs):
        requests.append(deepcopy(kwargs["json"]["messages"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": model_content(next(outputs))}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert run_model(model, "generate", "outline", payload, model_validator("outline", lambda output: None)) == good
    assert len(requests) == 2 and requests[1][:-2] == requests[0]
    assert requests[1][-2] == {"role": "assistant", "content": model_content({})}
    sent = json.loads(requests[0][-1]["content"])
    assert "ocr_texts" not in sent
    compact = deduplicate_ocr(payload)
    assert "anchors" not in sent
    assert sent == {**{k: v for k, v in compact.items() if k != "anchors"}, **{
        key: [{"id": item["id"], "text": item["text"]} for item in compact[key]]
        for key in ("evidence", "context")}}
    assert sent["context"] == []
    length = sum(len(item["text"]) for key in ("evidence", "context") for item in sent[key])
    assert model.input_characters("outline", payload) == message_characters(requests[0])
    assert length < sum(len(i["text"]) for key in ("evidence", "context") for i in payload[key])
    assert payload == before


def test_ocr_deduplication_preserves_citation_time_exports_and_successful_checkpoints(tmp_path, monkeypatch):
    from app.videos.notes import notes_text, notes_markdown
    evidence = materials()
    before = deepcopy(evidence)
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    coordinator = DetailedVideoGeneration("roundtrip", model, model.settings, tmp_path, evidence, 10,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    requests = []
    async def post(self, url, **kwargs):
        sent = json.loads(kwargs["json"]["messages"][-1]["content"])
        requests.append(sent)
        if "evidence" in sent:
            value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "滴定", "core_question": "如何操作", "topics": ["反应与条件"],
                                   "start_anchor_id": "E1"}], "video_summary": "介绍滴定。"}
        elif "chapters" in sent:
            assert sent["chapters"][0]["primary"][3] == {"id": "F4", "text": evidence[3]["text"].rstrip("\r\n")}
            value = {"chapters": [{'chapter_id': 'C1', 'summary': '摘要', "markdown": '**实验条件**：1-4mL。', "issues": [{'kind': 'source_issue', 'quote': 'AgCl / AgC1', 'reason': '字母不清', 'evidence_ids': ['F4']}]}]}
        else:
            value = {"title": "滴定", "tags": [], "overview_paragraphs": ["实验内容。"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": model_content(value)}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async def run():
        async with model:
            result = await coordinator.run()
            # 发送副本去重后，本地原文及同一流程的成功检查点仍可复用。
            with monkeypatch.context() as patch:
                patch.setattr("app.videos.model.deduplicate_ocr", lambda value: value)
                assert await coordinator.run() == result
            return result
    result = asyncio.run(run())
    assert model.calls == len(requests) == 3 and evidence == before
    block, issue = result["chapters"][0], result["source_issues"][0]
    assert block["evidence_ids"] == ["E1", "E2", "E3", "E4"] and issue["evidence_ids"] == ["E4"]
    assert (block["start_seconds"], block["end_seconds"], block["timestamp_basis"]) == (0, 10, "frame")
    assert "start_seconds" not in issue
    assert "1-4mL" in notes_text("实验", 10, result) and "AgCl / AgC1" in notes_markdown("实验", 10, result)
