from app.analysis.budgets import message_characters
import asyncio
import json
import httpx
import pytest
from app.config import Settings
from app.db import Task
from app.errors import AppError
from app.analysis.budgets import OUTPUT_TOKENS, restore_task_budgets
from app.settings.model_store import load_settings
from app.videos.model import ModelClient
from app.documents.model import DocumentModelClient
from app.settings.model_profiles import execution_settings
from app.videos.detailed import DetailedVideoGeneration
from app.videos.segments import split_segments
from .conftest import upload
from .fakes import FakeModel, FakeRecognizer

ROOT = "/api/v1/settings/model/profiles"
OUTPUT = {key: 2000 + index * 100 for index, key in enumerate(OUTPUT_TOKENS)}
VALUES = {"input_text_limit": 9000, "output_tokens": OUTPUT}


def test_profile_budgets_persist_and_do_not_bleed_between_profiles(client, app):
    created = client.post(ROOT, json={"name": "小预算", "model": "custom", **VALUES})
    assert created.status_code == 201, created.text
    profiles = created.json()["profiles"]
    identifier = profiles[-1]["id"]
    for key, value in VALUES.items():
        assert profiles[-1][key] == value
    assert profiles[0]["input_text_limit"] == 25000
    assert profiles[0]["output_tokens"] == OUTPUT_TOKENS
    assert client.post(f"{ROOT}/{identifier}/activate").status_code == 200
    loaded = load_settings(Settings(data_dir=app.state.pipeline.get_settings().data_dir))
    assert loaded.llm_input_text_limit == 9000
    assert loaded.llm_output_tokens == OUTPUT
    assert client.post(ROOT + "/default/activate").status_code == 200
    assert client.get("/api/v1/settings/model").json()["output_tokens"] == OUTPUT_TOKENS


@pytest.mark.parametrize("field", ["input_text_limit", *OUTPUT_TOKENS])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "2000"])
def test_budget_fields_require_positive_integers(client, field, value):
    body = {field: value} if field.endswith("text_limit") else {"output_tokens": {field: value}}
    assert client.put("/api/v1/settings/model", json=body).status_code == 422


def test_task_budget_restore_uses_only_current_snapshot():
    initial = Settings(llm_input_text_limit=6000, llm_output_tokens=OUTPUT)
    restored = restore_task_budgets(initial, {"llm_input_text_limit": 7000, "llm_output_tokens": OUTPUT_TOKENS})
    assert restored.llm_input_text_limit == 7000 and restored.llm_output_tokens == OUTPUT_TOKENS
    with pytest.raises(KeyError):
        restore_task_budgets(initial, {"input_budget": 7000})


@pytest.mark.parametrize("client_type", [ModelClient, DocumentModelClient], ids=["video", "document"])
@pytest.mark.parametrize("operation", [key for key in OUTPUT_TOKENS if key != "connection_test"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("json_mode,provider", [(False, "openai-compatible"), (True, "bailian")], ids=["plain", "json-bailian"])
def test_each_stage_sends_its_configured_output_limit(monkeypatch, client_type, operation, stream, json_mode, provider):
    sent = []
    def respond(request):
        assert request.method == "POST" and str(request.url) == "https://budget.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-budget"
        sent.append(json.loads(request.content))
        if stream:
            chunk = {"choices": [{"finish_reason": "stop", "delta": {"content": "mock result"}}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "mock result"}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def run():
        async with client_type(Settings(llm_key="fake-budget", llm_model="budget-model", llm_base_url="https://budget.invalid/v1",
                llm_provider=provider, llm_stream=stream, llm_json_mode=json_mode, llm_output_tokens=OUTPUT,
                summary_prompt="保留条件与例子。")) as model:
            payload = {"material": "中文原文"}
            messages = model.messages(operation, payload)
            assert model.input_characters(operation, payload) == message_characters(messages)
            assert await model.stage_request(operation, messages) == "mock result"
            expected = {"model": "budget-model", "messages": messages, "temperature": 0.2,
                        "max_tokens": OUTPUT[operation], "stream": stream}
            if json_mode and operation != "chapters":
                expected["response_format"] = {"type": "json_object"}
            if provider == "bailian":
                expected["enable_thinking"] = False
            assert sent == [expected]
    asyncio.run(run())


def test_connection_test_uses_draft_output_limit(client, monkeypatch):
    captured = []
    async def request(self, messages, **kwargs):
        captured.append(kwargs["max_tokens"])
        return '{"ok":true}'
    monkeypatch.setattr("app.analysis.llm.ModelClient.request", request)
    assert client.post(ROOT + "/test", json={"token": "fake-budget", "output_tokens": {"connection_test": 321}}).status_code == 200
    assert captured == [321]
    assert client.get("/api/v1/settings/model").json()["output_tokens"] == OUTPUT_TOKENS


def test_custom_budget_changes_segments_and_cache_keys(tmp_path):
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i * 100, "end_seconds": i * 100 + 10,
                 "text": "原" * 2000} for i in range(3)]
    assert len(split_segments(evidence, 400, 3000)[1]) == 3
    assert len(split_segments(evidence, 400, 25000)[1]) == 1
    def generation(settings):
        model = FakeModel(settings)
        return DetailedVideoGeneration("budget-task", model, settings, tmp_path, evidence, 400,
                              asyncio.Event(), lambda: None, lambda _: None, {})
    old = generation(Settings())
    custom = generation(Settings(llm_input_text_limit=3000, llm_output_tokens=OUTPUT))
    assert old.path("outline", {}).name != custom.path("outline", {}).name
    assert old.path("outline", {}).name != generation(Settings(llm_output_tokens=OUTPUT)).path("outline", {}).name
    assert not custom.fits("outline", {"evidence": custom.evidence})
    assert old.fits("outline", {"evidence": old.evidence})


def test_queued_video_and_retry_use_snapshot_after_settings_change(client, app, video_file, monkeypatch):
    assert client.put("/api/v1/settings/model", json={"token": "fake-budget", **VALUES}).status_code == 200
    video = upload(client, video_file)
    captured = []
    class Model(FakeModel):
        def __init__(self, settings):
            super().__init__(settings)
            captured.append((settings.llm_input_text_limit, settings.llm_input_text_limit, settings.llm_output_tokens))

        async def generate(self, operation, payload, validate):
            if len(captured) == 1 and operation == "outline":
                raise AppError("model_unavailable", "simulated failure")
            return await super().generate(operation, payload, validate)
    pipeline = app.state.pipeline
    pipeline.model_factory, pipeline.recognizer_factory = Model, FakeRecognizer
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, target, media_info=None: target.write_bytes(b"audio"))
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    client.put("/api/v1/settings/model", json={"input_text_limit": 20000, "output_tokens": OUTPUT_TOKENS})
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "failed"
    assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 202
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert captured == [(9000, 9000, OUTPUT), (9000, 9000, OUTPUT)]
    with app.state.sessions() as session:
        assert session.get(Task, task_id).config["llm_output_tokens"] == OUTPUT


def test_document_execution_ignores_current_profile_budget_changes():
    from app.documents.tasks import task_config
    from app.documents.tasks import preview
    original = Settings(llm_input_text_limit=9000)
    material = {"version": 2, "units": [{"id": "D1", "text": "条件", "format": "text", "label": "一", "locator": {}}]}
    config = task_config(original, "材料", preview(material, original, "材料"))
    restored = execution_settings(Settings(llm_input_text_limit=2000, llm_output_tokens=OUTPUT), config)
    assert restored.llm_input_text_limit == 9000 and restored.llm_output_tokens == OUTPUT_TOKENS


@pytest.mark.parametrize("pretty", [False, True])
def test_input_budget_counts_text_and_schema_without_structural_overhead(pretty):
    system = '系统提示词及 Schema：{"type":"object"}'
    payload = {"video_title": "视频标题", "content_type": "课程或教材", "anchors": ["E1" * 1000],
        "evidence": [{"id": "超长编号" * 1000, "kind": "speech", "start_seconds": 10, "text": '原文\n"公式"\\路径'}],
        "context": [{"id": "E0", "text": "前文"}],
        "previous_chapter_outline": {"title": "前章", "core_question": "为什么", "topics": ["条件", "过程"]},
        "chapter_id": "C1", "evidence_ids": ["E1"], "label": "幻灯片1"}
    content = json.dumps(payload, ensure_ascii=pretty, indent=2 if pretty else None)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "补充要求"},
                {"role": "user", "content": content}]
    assert message_characters(messages) == len(system) + sum(map(len, ["补充要求", "视频标题", "课程或教材",
        '原文\n"公式"\\路径', "前文", "前章", "为什么", "条件", "过程"]))


def test_input_budget_is_independent_of_record_count_and_ids():
    def count(items):
        return message_characters([{"role": "system", "content": "系统"}, {"role": "user", "content": json.dumps({"evidence": items})}])
    assert count([{"id": "E1", "text": "甲乙" * 500}]) == count([
        {"id": "很长的来源编号" + str(i), "text": "甲乙"} for i in range(500)]) == 1002
