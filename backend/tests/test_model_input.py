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


MATERIAL_PUNCTUATION = str.maketrans("：，；", ":,;")


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
    expected = {
        "E2": evidence[1]["text"].rstrip(),
        "E3": evidence[2]["text"].splitlines()[-1],
        "E4": evidence[3]["text"].rstrip() if operation != "outline" else evidence[3]["text"].splitlines()[-1],
    }
    assert {item["id"]: item["text"] for item in records if item["kind"] == "frame"} == {
        identifier: text.translate(MATERIAL_PUNCTUATION) for identifier, text in expected.items()
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


@pytest.mark.parametrize("operation", ["chapters", "extract", "concise"])
def test_duplicate_subtitle_is_removed_instead_of_encoded(operation):
    evidence = subtitle_materials()
    chapter = {"chapter_id": "C1", "primary": evidence, "context": []}
    payload = ({"chapters": [chapter]} if operation == "chapters" else
               {"chapter": chapter} if operation == "extract" else payload_for("视频", evidence, []))
    before = deepcopy(payload)
    sent = json.loads(ModelClient(Settings()).messages(operation, payload)[-1]["content"])
    assert "ocr_texts" not in sent
    records = (sent["chapters"][0] if operation == "chapters" else sent.get("chapter", sent))["primary"]
    table = "积分表:A 14;B 12"
    detail = "注意 Fe3+ 与 0.01mL。AgCl / AgC1。"
    if operation == "chapters":
        assert records.splitlines() == ["[New_S1]", f"> [F1] {table}{detail}", evidence[0]["text"]]
    else:
        speech, frame = records
        assert speech["text"] == evidence[0]["text"]
        assert frame["text"] == f"{table}{detail}"
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
    if operation == "chapters":
        assert records.startswith("[New_S1]\n> [F1] ")
        text_length = len(records)
        assert message_characters(messages[-1:]) == text_length
    else:
        assert all(isinstance(item["text"], str) for item in records)
        expected = evidence if operation == "outline" else deduplicate_ocr({"evidence": evidence})["evidence"]
        assert [(item["id"], item["text"]) for item in records] == [
            (item["id"], item["text"].replace("\r", "").replace("\n", "") if item["kind"] == "frame" else item["text"])
            for item in expected]
        text_length = sum(len(item["text"]) for item in records)
    assert "start_seconds" not in json.dumps(sent) and "end_seconds" not in json.dumps(sent)
    assert payload == before
    assert model.input_characters(operation, payload) == message_characters(messages)
    original_length = sum(len(item["text"]) for item in evidence)
    if operation == "outline":
        assert text_length == original_length - evidence[1]["text"].count("\n")
    else:
        assert text_length < original_length


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
                 "text": "第十回合，地图中间刷出星星，往后每二十回合刷一批\n实验结果 Fe3＋ 与 0．01mL。\n实验结果 Fe2＋ 与 0．1mL。"}]
    assert deduplicate_ocr({"evidence": evidence})["evidence"] == [
        evidence[0], {**evidence[1], "text": "实验结果 Fe3+ 与 0.01mL。"}]
    sent, _ = pack_chapter_material({"primary": evidence})
    assert [line for line in sent["primary"].splitlines() if line.startswith("> ")] == [
        "> [F1] 实验结果 Fe3+ 与 0.01mL。",
    ]


@pytest.mark.parametrize("operation", ["chapters", "extract"])
def test_actual_requests_budget_and_repair_use_deduplicated_material(tmp_path, monkeypatch, operation):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False,
                                 summary_prompt="保留实验条件"))
    model.material_dir = tmp_path / "materials"
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
    saved = list(model.material_dir.glob("*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text("utf-8")) == sent
    sent_chapter = sent["chapters"][0] if operation == "chapters" else sent["chapter"]
    if operation == "chapters":
        frames = [coordinator.evidence[0]["text"].replace("\r", "").replace("\n", "")]
        frames.extend(row["text"].splitlines()[-1] for row in coordinator.evidence[1:3])
        assert sent_chapter == {
            "chapter_id": "C1",
            "primary": "\n".join(f"> [F{index}] {text.translate(MATERIAL_PUNCTUATION)}"
                                 for index, text in enumerate(frames, 1)),
            "context": coordinator.evidence[3]["text"].translate(MATERIAL_PUNCTUATION).replace("\r", "").replace("\n", ""),
        }
    else:
        expected = deduplicate_ocr(chapter)
        assert sent_chapter == {**expected, **{
            key: [{"id": item["id"], "kind": item["kind"], "text": item["text"].replace("\r", "").replace("\n", "")}
                  for item in expected[key]]
            for key in ("primary", "context")}}
    assert model.input_characters(operation, payload) == message_characters(requests[0])
    assert payload == before and coordinator.path(operation, payload) == path






def test_input_budget_uses_deduplicated_material_and_reserves_repair(tmp_path, monkeypatch):
    model = ModelClient(Settings())
    coordinator = DetailedVideoGeneration("budget", model, model.settings, tmp_path, subtitle_materials(), 10,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    payload = {"video_summary": "当前片段主题", "chapters": [{"chapter_id": "C1", "primary": coordinator.evidence, "context": []}]}
    compressed_size = model.input_characters("chapters", payload)
    with monkeypatch.context() as patch:
        patch.setattr("app.videos.model_input._without_spoken_lines", lambda text, spoken, normalized_speech: text)
        full_size = model.input_characters("chapters", payload)
    assert compressed_size < full_size
    model.settings.llm_input_text_limit = compressed_size + model.repair_input_reserve
    assert coordinator.fits("chapters", payload)
    model.settings.llm_input_text_limit -= 1
    assert not coordinator.fits("chapters", payload)


def timed_record(identifier, kind, start, end, text):
    return {"id": identifier, "kind": kind, "start_seconds": start, "end_seconds": end, "text": text}


@pytest.mark.parametrize("operation", ["chapters", "extract", "concise"])
@pytest.mark.parametrize(("ocr", "speech", "retained"), [
    ("这次测量所需的样品质量为10", "这次测量所需的样品质量为100", True),
    ("这次测量所需的样品质量为10", "这次测量所需的样品质量为10.0", True),
    ("这次测量所需的样品质量为10", "这次测量所需的样品质量为10%", True),
    ("这次测量所需的样品质量为10", "这次测量所需的样品质量为10 mL", True),
    ("这次测量所需的样品体积为10", "这次测量所需的样品体积为10毫升", True),
    ("这次测量所需的样品体积为10 mL", "这次测量所需的样品体积为10 mL/min", True),
    ("0.5 V 是这次实验的参考电压", "电压显示为 - 0.5 V 是这次实验的参考电压", True),
    ("这次测量所需的样品体积为10 mL", "实验条件：这次测量所需的样品体积为10 mL。", False),
    ("本次实验生成的沉淀主要含有AgCl", "本次实验生成的沉淀主要含有AgClO", True),
    ("请把反应容器放在常温油浴中缓慢搅拌", "请把反应容器放在常温水浴中缓慢搅拌", True),
])
def test_sent_ocr_preserves_differences_from_speech(operation, ocr, speech, retained):
    frame = timed_record("F1", "frame", 1, 2, ocr)
    records = [timed_record("S1", "speech", 0, 3, speech), frame]
    chapter = {"primary": records, "context": []}
    payload = ({"chapters": [chapter]} if operation == "chapters" else
               {"chapter": chapter} if operation == "extract" else payload_for("视频", records, []))
    before = deepcopy(payload)
    sent = json.loads(ModelClient(Settings()).messages(operation, payload)[-1]["content"])
    if operation == "chapters":
        visible = [line for line in sent["chapters"][0]["primary"].splitlines() if line.startswith("> [F")]
        assert visible == ([f"> [F1] {ocr}"] if retained else [])
    else:
        visible = [item["text"] for item in sent.get("chapter", sent)["primary"] if item["kind"] == "frame"]
        assert visible == ([ocr] if retained else [])
    assert payload == before


@pytest.mark.parametrize("operation", ["chapters", "extract", "concise"])
def test_ocr_normalizes_symbols_before_deduplication_and_sending(operation):
    original = "（提示）ＡｇＳＣＮ会吸附Ag＋，用１．５ｍＬ试剂处理AgCl。"
    corrected = "(提示)ＡｇＳＣＮ会吸附Ag+,用１.５ｍＬ试剂处理AgCl。"
    new_line = "观察溶液颜色的变化。"
    different_term = corrected.replace("AgCl", "AgC1").replace("会吸附", "会及附")
    changed_quantity = corrected.replace("１.５", "１５")
    changed_charge = corrected.replace("Ag+", "Ag-")
    records = [
        timed_record(f"E{index}", "frame", index, index, text)
        for index, text in enumerate((original, f"{corrected}\n{new_line}",
                                      different_term, changed_quantity, changed_charge))
    ]
    before = deepcopy(records)
    chapter = {"chapter_id": "C1", "primary": records, "context": []}
    payload = ({"chapters": [chapter]} if operation == "chapters" else
               {"chapter": chapter} if operation == "extract" else payload_for("视频", records, []))

    sent = json.loads(ModelClient(Settings()).messages(operation, payload)[-1]["content"])
    if operation == "chapters":
        lines = [line.split("] ", 1)[1] for line in sent["chapters"][0]["primary"].splitlines()]
    else:
        lines = [row["text"] for row in sent.get("chapter", sent)["primary"]]
    assert lines == [corrected, new_line, different_term, changed_quantity, changed_charge]
    assert records == before


def test_chapter_normalizes_sentence_marks_and_keeps_distinct_formulas():
    exclamation = "仔细观察实验现象！继续翻到下一页。"
    reread = exclamation.replace("！", "!")
    formulas = ("取AgCl₂样品继续观察。", "取AgCl2样品继续观察。")
    records = [timed_record(f"E{index}", "frame", index, index, text)
               for index, text in enumerate((exclamation, reread, *formulas))]

    sent, sources = pack_chapter_material({"primary": records})

    assert sent["primary"].splitlines() == [
        f"> [F1] {reread}", f"> [F2] {formulas[0]}", f"> [F3] {formulas[1]}",
    ]
    assert list(sources.values()) == [[records[0]], [records[2]], [records[3]]]


def test_clipped_ocr_keeps_the_fragment_when_the_complete_line_is_in_another_chapter():
    original = timed_record("E1", "frame", 0, 2, "加入硝基苯试剂并充分搅拌，观察沉淀的转化")
    clipped = timed_record("E2", "frame", 3, 3, "观察沉淀的转化")
    following = {**original, "id": "E3", "start_seconds": 5, "end_seconds": 6}
    hints = {"E2": [[0, "E1", "E3"]]}

    sent, sources = pack_chapter_material({"primary": [original, clipped, following], "clipped_lines": hints})
    assert "> [F2] 观察沉淀的转化" not in sent["primary"]
    assert sources["F1"] == [original]
    assert "clipped_lines" not in sent

    sent, sources = pack_chapter_material({"start_seconds": 3, "primary": [clipped, following],
                                          "context": [original], "clipped_lines": hints})
    assert sent["primary"].startswith("> [F1] 观察沉淀的转化\n")
    assert sources["F1"] == [clipped]


def test_chapter_context_is_plain_timeline_text_and_crossing_frames_are_citable():
    context = [
        timed_record("E1", "speech", 41, 44, "接上前文"),
        timed_record("E2", "speech", 44, 46, "前一条说明"),
        timed_record("E3", "frame", 46, 56, "旧画面\n画面补充"),
        timed_record("E4", "speech", 48, 51, "接着说完"),
        timed_record("E5", "frame", 51, 64, "章首还在显示"),
        timed_record("E6", "frame", 55, 80, "跨过整章的标题"),
    ]
    chapter = {"start_seconds": 60, "end_seconds": 70, "context": context,
               "primary": [timed_record("E7", "speech", 60, 65, "本章原话")]}
    before = deepcopy(chapter)
    sent, sources = pack_chapter_material(chapter)
    crossing_text = "> [F1] 章首还在显示\n> [F2] 跨过整章的标题"
    assert sent == {
        "primary": f"{crossing_text}\n[New_S1]\n本章原话",
        "context": "接上前文 前一条说明\n旧画面画面补充\n接着说完",
    }
    assert sources == {"F1": [context[4]], "F2": [context[5]], "New_S1": chapter["primary"]}
    assert chapter == before

    extracted = {key: value for key, value in chapter.items() if key != "primary"}
    extracted["facts"] = [{"text": "本章事实"}]
    sent_after_extract, _ = pack_chapter_material(extracted)
    assert sent_after_extract["facts"] == extracted["facts"]
    assert sent_after_extract["primary"] == crossing_text
    assert sent_after_extract["context"] == sent["context"]


def test_chapter_material_inserts_frames_at_original_fragments_and_keeps_standalone_frames_in_order():
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
        "primary": "\n".join([
            "[New_S1]", "> [F1] Claude", "开场原话 第一条说明", "> [F2] 短时标牌", "第一块末句",
            "[New_S2]", "第二块原话", "> [F3] 独立画面",
            "[New_S3]", "超过一分钟的单条语音", "[New_S4]", "> [F4] Claude", "仍从新块开始",
        ]),
        "context": "本章前文",
    }
    assert [item["id"] for item in sources["New_S1"]] == ["E1", "E3", "E5"]
    assert sources["F1"] == [primary[1]] and sources["F4"] == [primary[-1]]
    assert chapter == before


def test_chapter_material_groups_frames_before_the_same_fragment_in_time_order():
    records = [
        timed_record("E1", "speech", 0, 6, "第一句"),
        timed_record("E2", "frame", 1, 5, "画面甲"),
        timed_record("E3", "frame", 2, 4, "画面乙"),
    ]
    sent, _ = pack_chapter_material({"primary": records})
    assert sent["primary"] == "[New_S1]\n> [F1] 画面甲\n> [F2] 画面乙\n第一句"


def test_chapter_material_inserts_frames_at_appearances_during_a_speech_gap():
    records = [
        timed_record("E1", "speech", 10, 12, "停顿前的话"),
        timed_record("E2", "frame", 15, 19, "过渡画面"),
        timed_record("E3", "speech", 18, 20, "停顿后的话"),
    ]
    sent, _ = pack_chapter_material({"primary": records})
    assert sent["primary"] == "[New_S1]\n停顿前的话\n> [F1] 过渡画面\n停顿后的话"


def test_chapter_filter_keeps_labels_values_and_context_separate():
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
    assert sent["primary"].splitlines() == [
        f"> [F1] Claude{number.replace('0.01', '0.1')}条件变化时保持 Fe3+ 而非 Fe2+",
        "[New_S1]", f"{first} {second} Claude {number}", f"> [F2] {primary[-1]['text']}",
    ]
    assert sent["context"] == context[0]["text"]


def test_chapter_filter_matches_exact_prose_across_visual_line_breaks():
    outside = "这句话只出现在相邻的语音块里。"
    quantity = "试验要求把溶液浓度保持在0.01"
    visual = "附加提醒是先保存副本。"
    different = "这份补充资料可以帮助橙理完整的复习笔记。"
    long_line = "我们还要提前核对每份申请所需要提交的全部"
    frame = timed_record("E4", "frame", 60, 62,
                         "本章、需要说明怎样\n整理这份原始材料。\n"
                         f"{long_line}\n证明材料。\n"
                         f"{different}{visual}\n"
                         f"{quantity}\n能仅凭画面判定整道题目的答案。\n{outside}")
    chapter = {"primary": [
        timed_record("E1", "speech", 31, 32, "本章需要说明怎样"),
        timed_record("E2", "speech", 38, 39, "整理这份原始材料"),
        timed_record("E3", "speech", 44, 46, long_line + "证明材料。"),
        frame,
        timed_record("E5", "speech", 87, 89, "这份补充资料可以帮助整理完整的复习笔记。"
                     f"{quantity}2。不能仅凭画面判定整道题目的答案。"),
        timed_record("E6", "speech", 91, 92, outside),
    ]}
    before = deepcopy(chapter)
    sent, sources = pack_chapter_material(chapter)
    assert [line for line in sent["primary"].splitlines() if line.startswith("> ")] == [
        f"> [F1] {different}{visual}{quantity}能仅凭画面判定整道题目的答案。{outside}",
    ]
    assert sources["F1"] == [frame] and chapter == before


def test_chapter_filter_uses_the_speech_block_where_the_frame_appears():
    first = "这里已经讲清楚如何核对整份原始材料。"
    following = "下一步会讲解怎样提交最后的复习笔记。"
    crossing = timed_record("E3", "frame", 58, 64, f"{first}\n{following}")
    gap = timed_record("E4", "frame", 60, 60, following)
    records = [
        timed_record("E1", "speech", 0, 4, first),
        timed_record("E2", "speech", 57, 59, "第一部分到这里结束。"),
        crossing,
        gap,
        timed_record("E5", "speech", 61, 66, following),
        timed_record("E6", "frame", 62, 63, following),
    ]
    sent, sources = pack_chapter_material({"primary": records})
    assert [line for line in sent["primary"].splitlines() if line.startswith("> ")] == [
        f"> [F1] {following}", f"> [F2] {following}",
    ]
    assert sources == {"New_S1": records[:2], "New_S2": [records[4]], "F1": [crossing], "F2": [gap]}


def test_chapter_filter_removes_spoken_short_phrases_and_continuous_repeated_sentences():
    age = "你的儿子今年十五岁。"
    first = timed_record("E2", "frame", 1, 1, age)
    label = timed_record("E3", "frame", 1.5, 2, "牢大")
    extra = timed_record("E6", "frame", 3, 3, age + "画面另有一条提醒。")
    later = timed_record("E8", "frame", 8, 8, age)
    records = [
        timed_record("E1", "speech", 0, 2.3, "你的儿子今年15岁"),
        first,
        label,
        timed_record("E4", "frame", 2, 2, age + "有点早熟，成"),
        timed_record("E5", "speech", 2.3, 4, "有点早熟成绩不错"),
        extra,
        timed_record("E7", "frame", 3.5, 4, "有点早熟，成绩不错。"),
        later,
        timed_record("E9", "speech", 9, 10, "最近在了解这项内容"),
    ]
    before = deepcopy(records)
    sent, sources = pack_chapter_material({"primary": records})
    assert [line for line in sent["primary"].splitlines() if line.startswith("> ")] == [
        f"> [F1] {age}", "> [F2] 牢大", "> [F3] 画面另有一条提醒。", f"> [F4] {age}",
    ]
    assert [sources[f"F{i}"] for i in range(1, 5)] == [[first], [label], [extra], [later]]
    assert records == before and "你的儿子今年15岁" in sent["primary"]


def test_chapter_deduplicates_identical_formula_lines_and_keeps_value_changes():
    formula = "入1-3mL硝基苯，使其包裹在AgCl沉淀表面"
    changed = formula.replace("1-3mL", "1-5mL")
    first = timed_record("E2", "frame", 1, 3, formula)
    clipped = timed_record("E3", "frame", 4, 6, f"{formula}\n观察沉淀转化")
    different = timed_record("E4", "frame", 6, 7, changed)
    later = timed_record("E5", "frame", 11, 12, formula)
    records = [timed_record("E1", "speech", 0, 15, "接着看这里的实验条件"), first, clipped, different, later]
    before = deepcopy(records)

    sent, sources = pack_chapter_material({"primary": records})

    assert [line for line in sent["primary"].splitlines() if line.startswith("> ")] == [
        f"> [F1] {formula.replace('，', ',')}", "> [F2] 观察沉淀转化",
        f"> [F3] {changed.replace('，', ',')}", f"> [F4] {formula.replace('，', ',')}",
    ]
    assert [sources[f"F{i}"] for i in range(1, 5)] == [[first], [clipped], [different], [later]]
    assert records == before


def test_chapter_aliases_resolve_before_partial_checkpoints_and_stay_stable_on_repair(tmp_path, monkeypatch):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    evidence = [
        timed_record("first", "speech", 0, 2, "甲先解释第一问"),
        timed_record("first-label", "frame", 3, 5, "甲的名牌"),
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
    assert [chapter["primary"] for chapter in sent[0]] == [
        "[New_S1]\n甲先解释第一问\n> [F1] 甲的名牌\n甲再解释第二问",
        "[New_S1]\n> [F1] 乙的名牌\n乙先解释第三问",
    ]
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



def test_outline_counts_every_record_with_flattened_ocr_during_repair(monkeypatch):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    evidence = materials()
    evidence.append({"id": "S1", "kind": "speech", "start_seconds": 0, "end_seconds": 1,
                     "text": "对比 Fe2+、Fe3+，\n注意 0.1 与 0.01 的区别。"})
    payload = {"anchors": [{"id": "S1", "start_seconds": 0}], "evidence": evidence,
               "context": {"video_summary": "前段概述", "chapters": [
                   {"title": "前段主题", "core_question": "前段问题", "topics": ["前段要点"]}]}}
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
    assert "anchors" not in sent
    assert sent == {"context": payload["context"], "evidence": [
        {"id": identifier, "text": item["text"].replace("\r", "").replace("\n", "")
         if item["kind"] == "frame" else item["text"]}
        for identifier, item in zip(("F1", "F2", "F3", "F4", "S1"), evidence, strict=True)]}
    overhead = model.input_characters("outline", {"evidence": [], "context": payload["context"]})
    assert model.input_characters("outline", payload) == message_characters(requests[0])
    assert message_characters(requests[0]) == overhead + sum(len(item["text"]) for item in sent["evidence"])
    assert payload == before


@pytest.mark.parametrize("repair_quote", [False, True])
def test_ocr_deduplication_preserves_citation_time_exports_and_successful_checkpoints(tmp_path, monkeypatch, repair_quote):
    from app.videos.notes import notes_text, notes_markdown
    evidence = materials()
    label = "AgCl / AgC1;1-4mL;Fe3+;0;第4次。"
    raw_quote = "AgCl ／\r\n AgC1"
    sent_quote = "AgCl / AgC1"
    evidence[-1]["text"] = evidence[-1]["text"].replace(sent_quote, raw_quote)
    before = deepcopy(evidence)
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False))
    model.material_dir = tmp_path / "note-materials" / "roundtrip"
    coordinator = DetailedVideoGeneration("roundtrip", model, model.settings, tmp_path, evidence, 10,
                                 asyncio.Event(), lambda: None, lambda stage: None, {})
    requests = []
    async def post(self, url, **kwargs):
        sent = json.loads(kwargs["json"]["messages"][-1]["content"])
        requests.append(sent)
        if "evidence" in sent:
            assert "AgCl ／ AgC1" in sent["evidence"][-1]["text"]
            value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "滴定", "core_question": "如何操作", "topics": ["反应与条件"],
                                   "start_anchor_id": "E1"}], "video_summary": "介绍滴定。"}
        elif "chapters" in sent:
            assert sent["chapters"][0]["primary"].endswith(f"\n> [F4] {label}")
            quote = "错误引文" if repair_quote else sent_quote
            value = {"chapters": [{'chapter_id': 'C1', 'summary': '摘要', "markdown": '**实验条件**：1-4mL。', "issues": [{'kind': 'source_issue', 'quote': quote, 'reason': '字母不清', 'evidence_ids': ['F4']}]}]}
        elif "repairs" in sent:
            assert sent_quote in sent["sources"][0]["text"] and "／" not in sent["sources"][0]["text"]
            value = {"repairs": [{"chapter_id": "C1", "block_index": 0, "quote": sent_quote}]}
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
    assert model.calls == len(requests) == 3 + repair_quote and evidence == before
    block, issue = result["chapters"][0], result["source_issues"][0]
    assert block["evidence_ids"] == ["E1", "E2", "E3", "E4"] and issue["evidence_ids"] == ["E4"]
    assert (block["start_seconds"], block["end_seconds"], block["timestamp_basis"]) == (0, 10, "frame")
    assert "start_seconds" not in issue
    assert issue["quote"] == sent_quote
    saved = list(model.material_dir.glob("chapters-*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text("utf-8")) == requests[1]
    assert "1-4mL" in notes_text("实验", 10, result) and "AgC1" in notes_markdown("实验", 10, result)
