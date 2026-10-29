from .fakes import model_validator
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.analysis.budgets import message_characters, OUTPUT_TOKENS
from app.analysis.content_types import CONTENT_TYPES
from app.config import Settings
from app.db import Task, DocumentTask
from app.documents.concise import ConciseDocumentGeneration
from app.documents.model import DocumentModelClient
from app.errors import AppError
from app.settings.model_store import load_settings
from app.videos.concise import ConciseVideoGeneration, validate_result
from app.errors import TaskPaused
from app.videos.model import ModelClient
from app.videos.pipeline import Pipeline
from .conftest import upload as upload_video
from .document_samples import sample
from .fakes import FakeRecognizer


@pytest.fixture
def concise_calls(monkeypatch):
    class Calls(list):
        hook = None
    calls = Calls()
    async def respond(request):
        body = json.loads(request.content)
        payload = next(json.loads(m["content"]) for m in body["messages"] if m["role"] == "user" and m["content"].startswith("{"))
        calls.append((body, payload))
        value = {"content_type": payload.get("content_type", "课程或教材"), "title": "主要概念与条件", "markdown": "**核心结论**：保留原文条件。"}
        if "video_title" in payload:
            value["markdown"] = f"## 核心概念 [回看](#source-{payload['primary'][0]['id']})\n\n" + value["markdown"]
        else:
            value["markdown"] = f"## 核心概念 [来源](#source-{payload['primary'][0]['id']})\n\n" + value["markdown"]
        if calls.hook:
            value = calls.hook(payload, value)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    return calls


def material(text="原文说明条件。"):
    return {"version": 2, "units": [{"id": "D1", "text": text, "label": "正文 1", "format": "text", "locator": {"block": 1}}]}


def settings(**kwargs):
    return Settings(llm_key="fake-style", llm_stream=False, note_style="concise", **kwargs)


@pytest.mark.parametrize("model_type", [ModelClient, DocumentModelClient])
@pytest.mark.parametrize("selected", CONTENT_TYPES)
def test_concise_type_guidance_only_sent_when_needed(model_type, selected):
    model = model_type(settings(summary_prompt="解释关键步骤"))
    payload = {"primary": [{"id": "S1", "kind": "speech", "text": "原文与代码。"}], "context": []}
    if model_type is DocumentModelClient:
        payload["primary"] = [{"id": "D1", "text": "原文与代码。"}]
    first = model.messages("concise", payload)
    assert all(first[0]["content"].count(value) == 1 for value in CONTENT_TYPES.values())
    schema = json.loads(first[0]["content"].split("只输出符合Schema的JSON：", 1)[1])
    assert schema["properties"]["content_type"]["enum"] == list(CONTENT_TYPES)
    assert "解释关键步骤" in first[1]["content"] and json.loads(first[-1]["content"]) == payload
    later_payload = {**payload, "content_type": CONTENT_TYPES[selected]}
    later = model.messages("concise", later_payload)
    schema = json.loads(later[0]["content"].split("只输出符合Schema的JSON：", 1)[1])
    assert schema["properties"]["content_type"]["enum"] == [selected]
    sent = "\n".join(m["content"] for m in later)
    assert sent.count(CONTENT_TYPES[selected]) == 1
    assert all(value not in sent for name, value in CONTENT_TYPES.items() if name != selected)
    assert model.input_characters("concise", later_payload) == message_characters(later)


@pytest.mark.parametrize("model_type", [ModelClient, DocumentModelClient])
@pytest.mark.parametrize("continuation", [False, True])
def test_concise_prompt_handles_short_tail_as_part_of_notes(model_type, continuation):
    model = model_type(settings())
    primary = {"id": "E1", "kind": "speech", "text": "希望大家提高警惕，欢迎评论交流。"}
    if model_type is DocumentModelClient:
        primary = {"id": "D1", "text": primary["text"]}
    payload = {"primary": [primary], "context": [{**primary, "text": "前文已经讲解攻击案例与防护措施。"}]}
    if continuation:
        payload["content_type"] = CONTENT_TYPES["观点讲解或评论"]
    messages = model.messages("concise", payload)
    prompt = messages[0]["content"]
    assert "只写 primary 的新增内容" in prompt and "不重建整篇结构" in prompt
    assert "不把当前片段未提及的内容说成整份材料没有" in prompt
    assert "不另写点评" in prompt
    assert "每节是一个具体标题和一段一至两句话的内容介绍" in prompt
    assert "不展开公式、解题步骤、计算答案或完整代码" in prompt
    assert "详细写作要求不用于展开本导览" in prompt
    assert "约 2000 字" not in prompt and "必要完整代码" not in prompt
    if model_type is ModelClient:
        assert "带时间戳的章节大纲" in prompt and "一至两句话" in prompt
        assert "课件列出的全章目录不代表本段都已讲解" in prompt
    else:
        assert "文档导览编辑" in prompt and "按原文顺序" in prompt
        assert "document_title 仅辅助理解" in prompt
        assert "文档目录不代表本段已包含所有章节的正文" in prompt
        assert "单处来源用[来源](#source-D1)" in prompt and "[来源](#source-D1-D3)" in prompt
        assert all(value not in prompt for value in ["时间戳", "视频时间", "#source-E", "video_title"])
    assert all(old not in prompt for old in ["三个清晰的二级标题区块", "一、基本概念与原理", "二、例题与应用", "三、易错点与复习要点"])
    sent = json.loads(messages[-1]["content"])
    assert sent["primary"][0]["text"] == primary["text"]
    assert sent["context"][0]["text"].startswith("前文已经讲解")
    assert model.input_characters("concise", payload) == message_characters(messages)


@pytest.mark.parametrize("extra", [0, 1])
def test_actual_message_boundary_checked_before_count_and_http(concise_calls, extra):
    async def run():
        async with ModelClient(settings()) as model:
            payload = {"primary": [{"id": "E1", "kind": "speech", "text": "中文与代码 print(1)"}], "context": []}
            messages = model.messages("concise", payload)
            model.settings.llm_input_text_limit = message_characters(messages) - extra
            counted = []
            model.before_request = lambda: counted.append(True)
            if extra:
                with pytest.raises(AppError) as error:
                    await model.stage_request("concise", messages)
                assert error.value.code == "model_input_budget" and not counted and not concise_calls
            else:
                await model.stage_request("concise", messages)
                assert len(counted) == len(concise_calls) == model.calls == 1
    asyncio.run(run())


def test_repair_over_budget_does_not_increase_request_count(concise_calls):
    concise_calls.hook = lambda payload, value: {}
    async def run():
        async with ModelClient(settings()) as model:
            payload = {"primary": [{"id": "E1", "kind": "speech", "text": "材料"}], "context": []}
            model.settings.llm_input_text_limit = model.input_characters("concise", payload)
            counted = []
            model.before_request = lambda: counted.append(True)
            with pytest.raises(AppError) as error:
                await model.generate("concise", payload, model_validator("concise", lambda v: v))
            assert error.value.code == "model_input_budget"
            assert len(concise_calls) == len(counted) == model.calls == 1
    asyncio.run(run())


@pytest.mark.parametrize("selected", CONTENT_TYPES)
def test_one_call_video_type_complete_and_no_hard_length_limit(tmp_path, concise_calls, selected):
    concise_calls.hook = lambda payload, value: {**value, "content_type": selected, "markdown": value["markdown"] + "正文。" * 800}
    async def run():
        async with ModelClient(settings()) as model:
            evidence = [{"id": "s1", "kind": "speech", "text": "材料原文", "start_seconds": 0, "end_seconds": 10}]
            gen = ConciseVideoGeneration("test", model, model.settings, tmp_path, evidence, 7200, asyncio.Event(), lambda: None,
                                         lambda _: None, {}, lambda _: None, "材料")
            result = await gen.run()
            assert len(concise_calls) == 1 and result["total_segments"] == 1
            assert result["content_type"] == CONTENT_TYPES[selected]
            assert len(result["chapters"][0]["markdown"]) > 2000
            assert all(result[k] == [] for k in ["tags", "overview_paragraphs", "source_issues", "segment_guides"])
            from app.videos.notes import notes_markdown, notes_text
            for text in (notes_markdown("材料.mp4", 7200, result), notes_text("材料.mp4", 7200, result)):
                assert text.count("主要概念与条件") == 1
                assert "整体概览" not in text and "暂无标签" not in text
    asyncio.run(run())


def test_concise_video_sends_compact_material_and_keeps_local_sources(tmp_path, concise_calls):
    async def run():
        async with ModelClient(settings()) as model:
            evidence = [{"id": f"{'a' * 32}-{'b' * 16}-speech-{i}", "kind": "speech",
                "text": f"第{i}项原理。" + "解释与条件。" * 15, "start_seconds": i * 4, "end_seconds": i * 4 + 3,
                "image_name": None, "confidence": None} for i in range(145)]
            original = deepcopy(evidence)
            result = await ConciseVideoGeneration("compact", model, model.settings, tmp_path, evidence, 600,
                asyncio.Event(), lambda: None, lambda _: None, {}, lambda _: None, "材料").run()
            assert len(concise_calls) == result["total_segments"] == 1
            body, payload = concise_calls[0]
            assert payload["primary"] == [{"id": f"E{i + 1}", "kind": item["kind"], "text": item["text"]}
                                          for i, item in enumerate(evidence)]
            assert message_characters(body["messages"]) <= model.settings.llm_input_text_limit
            assert result["chapters"][0]["evidence_ids"] == [item["id"] for item in evidence]
            manifest = json.loads(next(tmp_path.glob("*concise-manifest*.json")).read_text("utf-8"))
            assert manifest["segments"][0]["primary"][0] == {**evidence[0], "start_offset": 0,
                "end_offset": len(evidence[0]["text"])}
            validate_result(result, {item["id"]: item for item in evidence}, 600)
            assert evidence == original
    asyncio.run(run())


def test_concise_video_compact_ids_match_context_and_primary():
    from app.videos.concise import payload_for
    primary = [{"id": "original-speech", "kind": "speech", "text": "后半段", "start_offset": 3, "end_offset": 6}]
    context = [{"id": "original-frame", "kind": "frame", "text": "前方画面", "image_name": "frame.jpg"},
               {**primary[0], "text": "前半段", "start_offset": 0, "end_offset": 3}]
    original = deepcopy((primary, context))
    payload = payload_for("材料", primary, context)
    assert payload["primary"] == [{"id": "E1", "kind": "speech", "text": "后半段"}]
    assert payload["context"] == [{"id": "E2", "kind": "frame", "text": "前方画面"},
                                   {"id": "E1", "kind": "speech", "text": "前半段"}]
    assert (primary, context) == original


def test_split_same_speech_pause_resume_preserves_manifest_and_cursor(tmp_path, concise_calls, monkeypatch):
    async def run():
        async with ModelClient(settings(llm_input_text_limit=5500)) as model:
            evidence = [{"id": "long", "kind": "speech", "text": "第一句解释。\n" * 1600, "start_seconds": 0, "end_seconds": 600}]
            original, drafts = deepcopy(evidence), []
            stopped = asyncio.Event()
            def publish(value):
                drafts.append(deepcopy(value))
                stopped.set()
            def make(stop, callback):
                return ConciseVideoGeneration("partial", model, model.settings, tmp_path, evidence, 600, stop,
                    lambda: None, lambda _: None, {}, callback, "材料")
            with pytest.raises(TaskPaused):
                await make(stopped, publish).run()
            first = drafts[0]
            assert first["covered_until_seconds"] == 0 and 0 < first["source_cursor"]["offset"] < len(evidence[0]["text"])
            assert not first["complete"] and first["chapters"][0]["end_seconds"] == 600
            before = next(tmp_path.glob("*concise-manifest*.json")).read_bytes()
            monkeypatch.setattr("app.videos.concise.plan_segments", lambda *a: pytest.fail("must read fixed manifest"))
            result = await make(asyncio.Event(), drafts.append).run()
            assert result["complete"] and result["covered_until_seconds"] == 600
            assert result["chapters"][0] == first["chapters"][0]
            assert next(tmp_path.glob("*concise-manifest*.json")).read_bytes() == before
            assert len(concise_calls) == result["total_segments"]
            assert all(p["content_type"] == CONTENT_TYPES["课程或教材"] for _, p in concise_calls[1:])
            assert all(message_characters(body["messages"]) <= 5500 for body, _ in concise_calls)
            validate_result(result, {e["id"]: e for e in evidence}, 600)
            damaged = deepcopy(result)
            damaged["chapters"][1]["evidence_spans"][0]["start_offset"] -= 1
            with pytest.raises(AppError):
                validate_result(damaged, {e["id"]: e for e in evidence}, 600)
            assert evidence == original
    asyncio.run(run())


def test_document_preview_execution_and_500_character_context_match(tmp_path, concise_calls):
    from app.documents.model import DocumentModelClient
    from app.documents.tasks import task_config
    from app.documents.tasks import preview
    async def run():
        source = material("条件与解释。\n" * 1800)
        async with DocumentModelClient(settings(llm_input_text_limit=5500)) as model:
            info = preview(source, model.settings, "课程.txt")
            config = task_config(model.settings, "课程.txt", info)
            snapshots = []
            gen = ConciseDocumentGeneration("doc", tmp_path, config, source, model, asyncio.Event(), lambda: None,
                                             lambda *a: None, snapshots.append)
            result = await gen.run()
            assert result["complete"] and result["total_segments"] == info["estimated_segments"] == len(concise_calls)
            assert all(sum(len(i["text"]) for i in p["context"]) <= 500 for _, p in concise_calls)
            assert result["covered_until_offset"] == len(source["units"][0]["text"])
            assert all(not value["complete"] for value in snapshots)
    asyncio.run(run())


@pytest.mark.parametrize("extension", ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"])
def test_document_concise_api_source_export_and_default_override(client, app, concise_calls, extension):
    client.put("/api/v1/settings/model", json={"token": "fake-style", "stream": False})
    assert client.get("/api/v1/settings/note-style").json()["note_style"] == "detailed"
    doc = client.post("/api/v1/documents", files={"file": ("课程." + extension, sample(extension))}).json()
    uri = f'/api/v1/documents/{doc["id"]}/analyses'
    response = client.post(uri, json={"note_style": "concise"})
    assert response.status_code == 202, response.text
    identifier = response.json()["task_id"]
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "succeeded", status
    assert status["request_count"] == 1 and status["note_style"] == "concise"
    result = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert result["note_style"] == "concise" and result["complete"]
    ids = result["chapters"][0]["source_ids"]
    assert f"#document-unit-{ids[0]}" in result["chapters"][0]["markdown"]
    preview = client.get(f'/api/v1/documents/{doc["id"]}/content', params={"limit": 100})
    assert preview.status_code == 200 and set(ids) <= {u["id"] for u in preview.json()["units"]}
    for fmt in ["txt", "md"]:
        text = client.get(f"/api/v1/document-analyses/{identifier}/notes?format={fmt}").text
        assert text.count("主要概念与条件") == 1
        assert result["chapters"][0]["source_labels"][0] in text
        assert "#document-unit-" not in text and "#source-" not in text
    assert client.get("/api/v1/settings/note-style").json()["note_style"] == "detailed"


def test_effective_style_and_prompt_snapshot_and_old_task_gates(client, app):
    client.put("/api/v1/settings/model", json={"token": "fake-style"})
    doc = client.post("/api/v1/documents", files={"file": ("课程.txt", "条件。".encode())}).json()
    uri = f'/api/v1/documents/{doc["id"]}/analyses'
    first = client.post(uri).json()["task_id"]
    assert client.post(uri, json={"note_style": "detailed"}).json()["task_id"] == first
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "新偏好", "note_style": "concise"})
    assert client.post(uri).status_code == 409
    with app.state.sessions() as session:
        saved = deepcopy(session.get(DocumentTask, first).config)
    assert saved["note_style"] == "detailed" and saved["summary_prompt"] == ""
    client.post(f"/api/v1/document-tasks/{first}/stop")
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, first)
        task.config = {**task.config, "generation_flow": "old"}
        old = deepcopy(task.config)
    assert client.post(f"/api/v1/document-tasks/{first}/resume").status_code == 409
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, first)
        assert task.config == old
        task.status = "queued"
    app.state.document_pipeline.run(first)
    assert client.get(f"/api/v1/document-tasks/{first}").json()["status"] == "failed"


def test_video_effective_config_and_version_rejection_before_recognition(client, app, video_file):
    video = upload_video(client, video_file)
    uri = f'/api/v1/videos/{video["id"]}/analyses'
    identifier = client.post(uri).json()["task_id"]
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "新偏好", "note_style": "concise"})
    assert client.post(uri).status_code == 409
    with app.state.sessions.begin() as session:
        task = session.get(Task, identifier)
        assert task.config["note_style"] == "detailed" and task.config["summary_prompt"] == ""
        task.config = {**task.config, "generation_flow": "old"}
        old = deepcopy(task.config)
        task.status = "paused"
    assert client.post(f"/api/v1/tasks/{identifier}/resume").status_code == 409
    with app.state.sessions.begin() as session:
        session.get(Task, identifier).status = "failed"
    assert client.post(f"/api/v1/tasks/{identifier}/retry").status_code == 409
    with app.state.sessions.begin() as session:
        session.get(Task, identifier).status = "queued"
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda _: pytest.fail("no recognition"), ModelClient)
    pipeline.run(identifier)
    with app.state.sessions() as session:
        task = session.get(Task, identifier)
        assert task.status == "failed" and task.config == old


def test_video_concise_api_reads_sources_and_exports_after_queue_settings_change(client, app, video_file, concise_calls):
    client.put("/api/v1/settings/model", json={"token": "fake-style", "stream": False})
    video = upload_video(client, video_file)
    uri = f'/api/v1/videos/{video["id"]}/analyses'
    response = client.post(uri, json={"note_style": "concise"})
    assert response.status_code == 202, response.text
    identifier = response.json()["task_id"]
    assert client.post(uri, json={"note_style": "concise"}).json()["task_id"] == identifier
    client.put("/api/v1/settings/note-style", json={"note_style": "detailed", "summary_prompt": "排队后的新偏好"})
    assert client.post(uri, json={"note_style": "concise"}).status_code == 409
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, ModelClient)
    pipeline.run(identifier)
    status = client.get(f"/api/v1/tasks/{identifier}").json()
    assert status["status"] == "succeeded" and status["note_style"] == "concise", status
    result = client.get(f"/api/v1/analyses/{identifier}").json()
    assert result["note_style"] == "concise" and result["complete"] and len(concise_calls) == 1, result
    assert result["model_requests"] == 1
    assert "排队后的新偏好" not in json.dumps(concise_calls)
    assert client.get(f'/api/v1/videos/{video["id"]}/evidence?task_id={identifier}').status_code == 200
    for fmt in ["txt", "md"]:
        response = client.get(f"/api/v1/analyses/{identifier}/notes?format={fmt}")
        assert response.status_code == 200 and response.text.count("主要概念与条件") == 1


def test_concise_continuation_type_change_repairs_only_current_segment(tmp_path, concise_calls):
    changed = False
    def change_once(payload, value):
        nonlocal changed
        if payload.get("content_type") and not changed:
            changed = True
            return {**value, "content_type": "经验分享"}
        return value
    concise_calls.hook = change_once
    async def run():
        async with ModelClient(settings(llm_input_text_limit=5500)) as model:
            source = [{"id": str(i), "kind": "speech", "text": "原文" * 500, "start_seconds": i * 200, "end_seconds": i * 200 + 10} for i in range(2)]
            single = {"video_title": "材料", "primary": [{**source[0], "start_offset": 0, "end_offset": 1000}], "context": []}
            model.settings.llm_input_text_limit = max(model.input_characters("concise", single),
                *(model.input_characters("concise", {**single, "content_type": value}) for value in CONTENT_TYPES.values())) + 300
            gen = ConciseVideoGeneration("repair", model, model.settings, tmp_path, source, 400, asyncio.Event(),
                lambda: None, lambda _: None, {}, lambda _: None, "材料")
            result = await gen.run()
            assert changed and result["total_segments"] == 2 and result["content_type"] == CONTENT_TYPES["课程或教材"]
            assert len(concise_calls) == result["total_segments"] + 1
            assert concise_calls[1][1] == concise_calls[2][1]
            assert "content_type_must_match_planned_video_type" in concise_calls[2][0]["messages"][-1]["content"]
            assert all(body["max_tokens"] == model.settings.llm_output_tokens["chapters"] for body, _ in concise_calls)
    asyncio.run(run())


def test_document_pause_resume_reuses_manifest_and_complete_body(tmp_path, concise_calls, monkeypatch):
    from app.errors import DocumentStopped
    from app.documents.model import DocumentModelClient
    from app.documents.tasks import task_config
    from app.documents.tasks import preview
    async def run():
        source = material("原文条件与解释。" * 1600)
        async with DocumentModelClient(settings(llm_input_text_limit=5500)) as model:
            config = task_config(model.settings, "课程", preview(source, model.settings, "课程"))
            stopped, drafts = asyncio.Event(), []
            def publish(value):
                drafts.append(deepcopy(value))
                stopped.set()
            def make(stop, callback):
                return ConciseDocumentGeneration("paused", tmp_path, config, source, model, stop,
                    lambda: None, lambda *a: None, callback)
            with pytest.raises(DocumentStopped):
                await make(stopped, publish).run()
            first = drafts[0]
            assert 0 < first["source_cursor"]["offset"] < len(source["units"][0]["text"])
            monkeypatch.setattr("app.documents.concise.plan", lambda *a: pytest.fail("must reuse manifest"))
            result = await make(asyncio.Event(), drafts.append).run()
            assert result["complete"] and result["chapters"][0] == first["chapters"][0]
            assert len(concise_calls) == result["total_segments"]
    asyncio.run(run())


@pytest.mark.parametrize("invalid", [False, True])
def test_single_profile_budget_consolidation_validates_before_replace(tmp_path, invalid):
    from pydantic import ValidationError
    from app.util.private_storage import read_private_json, decode_private
    path = tmp_path / "_private" / "model-settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"outline_text_limit": 3000, "input_text_limit": 7000 if not invalid else 0,
        "token": "", "clear_token": True, "output_tokens": {**OUTPUT_TOKENS, "chapters": 14000}}), "utf-8")
    before = path.read_bytes()
    if invalid:
        with pytest.raises(ValidationError):
            load_settings(Settings(data_dir=tmp_path))
        assert path.read_bytes() == before
        assert not list(path.parent.glob("*.bak"))
    else:
        value = load_settings(Settings(data_dir=tmp_path, llm_key="environment-fake"))
        assert value.llm_key == "" and value.llm_input_text_limit == 7000 and value.llm_output_tokens["chapters"] == 14000
        assert "outline_text_limit" not in read_private_json(path)
        assert decode_private(next(path.parent.glob("*.bak")).read_bytes()) == (before, True)


@pytest.mark.parametrize("fail", [False, True])
def test_config_consolidation_all_profiles_and_atomic_failure(tmp_path, monkeypatch, fail):
    from app.util.private_storage import read_private_json, decode_private
    folder = tmp_path / "_private"
    folder.mkdir()
    path = folder / "model-settings.json"
    value = {"version": 2, "active_id": "one", "summary_prompt": "偏好", "profiles": [
        {"id": "one", "name": "一", "token": "fake-secret", "outline_text_limit": 3000, "input_text_limit": 12000},
        {"id": "two", "name": "二", "token": "", "clear_token": True, "outline_text_limit": 4000,
         "input_text_limit": 16000, "output_tokens": {**OUTPUT_TOKENS, "chapters": 15000}}]}
    path.write_text(json.dumps(value), "utf-8")
    before = path.read_bytes()
    if fail:
        monkeypatch.setattr("app.settings.model_store.write_settings", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            load_settings(Settings(data_dir=tmp_path))
        assert path.read_bytes() == before
    else:
        loaded = load_settings(Settings(data_dir=tmp_path))
        assert loaded.llm_key == "fake-secret" and loaded.llm_input_text_limit == 12000
        saved = read_private_json(path)
        assert all("outline_text_limit" not in p for p in saved["profiles"])
        assert saved["profiles"][1]["token"] == "" and saved["profiles"][1]["clear_token"]
        assert saved["profiles"][1]["input_text_limit"] == 16000
        assert saved["profiles"][1]["output_tokens"]["chapters"] == 15000
        load_settings(Settings(data_dir=tmp_path))
    backups = list(folder.glob("*.bak"))
    assert len(backups) == 1 and decode_private(backups[0].read_bytes()) == (before, True)
