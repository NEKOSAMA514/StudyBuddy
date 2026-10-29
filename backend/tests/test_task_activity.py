from .fakes import model_validator
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import event

from app.analysis import activity
from app.analysis.activity import TaskActivity, in_session, public_activity, RUNTIME_KEY
from app.config import Settings
from app.db import Task, DocumentTask, Video, Document
from app.diagnostics import Diagnostics
from app.documents.model import DocumentModelClient
from app.documents.versions import generation_key
from app.videos.model import ModelClient


@pytest.fixture(params=[Task, DocumentTask], ids=["video", "document"])
def stored(app, request):
    table = request.param
    with app.state.sessions.begin() as session:
        if table is Task:
            session.add(Video(id="file", fingerprint="file", filename="file.mp4", size_bytes=1, duration=10,
                              video_codec="h264", audio_codec=None))
            session.flush()
            task = Task(id="job", video_id="file", status="running", config={"model": "fixed"})
        else:
            session.add(Document(id="file", fingerprint="file", filename="file.txt", format="txt", size_bytes=1,
                                 unit_count=1, character_count=1))
            session.flush()
            task = DocumentTask(id="job", document_id="file", status="running", config={"model": "fixed"})
        session.add(task)
    reporter = TaskActivity(app.state.sessions, table, "job", 1, app.state.diagnostics)
    def read():
        with app.state.sessions() as session:
            return session.get(table, "job")
    return table, reporter, read


def test_history_truncation_keeps_active_requests_and_final_error(app, stored):
    table, report, read = stored
    report("waiting", request_id="slow", operation="outline")
    for number in range(105):
        report("preparing", message=f"阶段 {number}")
    snapshot = public_activity(read())
    assert len(snapshot["history"]) == 100 and snapshot["dropped"] == 6
    assert snapshot["requests"][0]["id"] == "slow"
    assert snapshot["requests"][0]["elapsed_seconds"] >= 0
    with app.state.sessions.begin() as session:
        task = session.get(table, "job")
        task.status, task.error = "failed", {"message": "失败", "error_id": "final"}
        in_session(session, task, app.state.diagnostics)
    snapshot = public_activity(read())
    assert snapshot["requests"] == [] and snapshot["final_error"]["error_id"] == "final"
    assert snapshot["history"][-1]["level"] == "error" and "失败" in snapshot["history"][-1]["message"]
    report("receiving", request_id="slow")
    assert public_activity(read()) == snapshot


def test_resume_rejects_old_epoch_even_after_running_again(app, stored):
    table, old, read = stored
    old("waiting", request_id="old", operation="overview")
    with app.state.sessions.begin() as session:
        task = session.get(table, "job")
        task.attempts, task.status = 2, "queued"
        in_session(session, task, app.state.diagnostics)
    assert public_activity(read())["requests"] == []
    with app.state.sessions.begin() as session:
        session.get(table, "job").status = "running"
    current = TaskActivity(app.state.sessions, table, "job", 2, app.state.diagnostics)
    current("waiting", request_id="new", operation="concise")
    snapshot = public_activity(read())
    old("receiving", request_id="old", operation="overview")
    old("request_done", request_id="new")
    after = public_activity(read())
    assert [r["id"] for r in after["requests"]] == ["new"]
    assert after["sequence"] == snapshot["sequence"] and after["attempt"] == 2


def test_each_actual_send_resets_wait_but_receiving_keeps_send_time(stored, monkeypatch):
    _, report, read = stored
    stamps = iter(f"2026-10-24T01:00:0{number}+00:00" for number in range(4))
    monkeypatch.setattr(activity, "now", lambda: next(stamps))
    report("waiting", request_id="r", operation="outline", network_attempt=1)
    sent = public_activity(read())["requests"][0]["sent_at"]
    report("receiving", request_id="r", operation="outline", network_attempt=1)
    assert public_activity(read())["requests"][0]["sent_at"] == sent
    report("retry_wait", request_id="r", operation="outline", network_attempt=1)
    assert public_activity(read())["requests"][0]["state_since"].endswith("02+00:00")
    report("waiting", request_id="r", operation="outline", network_attempt=2)
    assert public_activity(read())["requests"][0]["sent_at"].endswith("03+00:00")


def test_parallel_writers_merge_and_business_config_cannot_erase_activity(app, stored):
    table, report, read = stored
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda number: report("waiting", request_id=str(number), operation="chapters"), range(8)))
    assert len(public_activity(read())["requests"]) == 8
    stale = {"model": "fixed", RUNTIME_KEY: {"history": ["stale"]}}
    if table is Task:
        app.state.pipeline.update("job", config=stale, stage="summarizing:1/8")
    else:
        app.state.document_pipeline.stage("job", "summarizing", 1, 2)
    assert len(public_activity(read())["requests"]) == 8
    assert read().config["model"] == "fixed"
    assert activity.merge_config(read().config, {"model": "new"})[RUNTIME_KEY] == read().config[RUNTIME_KEY]
    assert generation_key(stale) == generation_key({"model": "fixed"})
    report("request_done", request_id="0")
    assert len(public_activity(read())["requests"]) == 7


@pytest.mark.parametrize("failure", ["callback", "serialize", "write"])
def test_activity_failure_does_not_poison_terminal_commit(app, stored, monkeypatch, failure):
    table, report, read = stored
    report("waiting", request_id="slow", operation="outline")
    if failure == "callback":
        monkeypatch.setattr(activity, "apply", lambda *a, **k: (_ for _ in ()).throw(ValueError("event failure")))
    elif failure == "serialize":
        original = activity.apply
        monkeypatch.setattr(activity, "apply", lambda *a, **k: {**original(*a, **k), "bad": object()})
    else:
        engine = app.state.sessions.kw["bind"]
        def fail_update(connection, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE") and "config=" in statement:
                raise RuntimeError("isolated activity write failure")
        event.listen(engine, "before_cursor_execute", fail_update)
    try:
        report("receiving", request_id="slow")
        with app.state.sessions.begin() as session:
            task = session.get(table, "job")
            task.status, task.error = "succeeded", None
            in_session(session, task, app.state.diagnostics)
        assert read().status == "succeeded"
        assert public_activity(read())["requests"] == []
    finally:
        if failure == "write":
            event.remove(engine, "before_cursor_execute", fail_update)


def test_public_contract_redacts_and_does_not_return_internal_config(app, stored):
    table, report, read = stored
    assert public_activity(read()) is None
    report("retry_wait", request_id="r", operation="outline", message="token=sk-secret123456 " + "长" * 500,
           error={"message": "Authorization: Bearer secret", "error_id": "id", "raw": "private"})
    snapshot = public_activity(read())
    assert len(snapshot["current"]["message"]) <= 300
    assert "sk-secret" not in json.dumps(snapshot) and "private" not in json.dumps(snapshot)
    path = "/api/v1/tasks/job" if table is Task else "/api/v1/document-tasks/job"
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["activity"]["requests"][0]["id"] == "r"
        assert RUNTIME_KEY not in body["config"] and "model" not in body["config"]


@pytest.mark.parametrize("model_type", [ModelClient, DocumentModelClient], ids=["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
@pytest.mark.parametrize("stream", [True, False])
def test_model_activity_wait_receive_validate_and_callback_failure(tmp_path, monkeypatch, model_type, style, stream):
    model = model_type(Settings(llm_key="fake-only", llm_stream=stream))
    states = []
    model.diagnostics = Diagnostics(tmp_path)
    def record(state, **details):
        states.append((state, dict(details)))
    model.activity = record
    operation = "concise" if style == "concise" else "overview"
    value = ({"title": "导览", "content_type": "课程或教材", "markdown": "## 主题\n\n本节介绍概念。"} if style == "concise" else
             {"title": "概览", "tags": [], "overview_paragraphs": ["介绍核心议题。"]})
    answer = json.dumps(value, ensure_ascii=False)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
            assert "receiving" not in [s for s, _ in states]
            yield ('data: ' + json.dumps({"choices": [{"delta": {"content": answer}}]}) + '\n\n').encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    def transport(request):
        assert states[-1][0] == "waiting"
        return httpx.Response(200, stream=Body()) if stream else httpx.Response(200, json={
            "choices": [{"message": {"content": answer}, "finish_reason": "stop"}]})
    async def scenario():
        async with model:
            await model.client.aclose()
            model.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
            result = await model.generate(operation, {}, model_validator(operation, lambda value: value))
            assert result["title"] == value["title"]
    asyncio.run(scenario())
    names = [s for s, _ in states]
    assert names == (["pacing", "waiting", "receiving", "validating", "validated", "request_done"] if stream else
                     ["pacing", "waiting", "validating", "validated", "request_done"])
    assert len({d["request_id"] for _, d in states}) == 1
    states.clear()
    def broken(state, **details):
        states.append((state, details))
        raise ValueError("broken activity callback")
    model.activity = broken
    asyncio.run(scenario())
    assert model.calls == 2


def test_complete_response_validation_failure_is_repair_not_saved(tmp_path):
    model = ModelClient(Settings(llm_key="fake-only", llm_stream=False, llm_request_interval_seconds=0))
    model.diagnostics = Diagnostics(tmp_path)
    states, sends = [], []
    model.activity = lambda state, **details: states.append((state, dict(details)))
    def respond(request):
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        content = '{}' if len(sends) == 2 else json.dumps({"title": "概览", "tags": [], "overview_paragraphs": ["主线"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
    async def scenario():
        async with model:
            await model.client.aclose()
            model.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            await model.generate("overview", {}, model_validator("overview", lambda value: value))
    asyncio.run(scenario())
    names = [state for state, _ in states]
    assert "retry_wait" in names and "repair" in names and "saved" not in names
    assert names.index("validating") < names.index("repair") < names.index("validated")
    assert [(d.get("repair", 0), d["network_attempt"]) for s, d in states if s == "waiting"] == [(0, 1), (0, 2), (1, 1)]
    assert sends[-1]["messages"][-2]["role"] == "assistant"


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
def test_all_cached_generation_emits_no_request_activity(tmp_path, monkeypatch, kind, style):
    from app.analysis.content_types import CONTENT_TYPES
    from app.documents.tasks import preview, task_config
    from app.documents.concise import ConciseDocumentGeneration
    from app.documents.detailed import DetailedDocumentGeneration
    from app.videos.concise import ConciseVideoGeneration
    from app.videos.detailed import DetailedVideoGeneration
    from .fakes import FakeModel
    class Model(FakeModel):
        def messages(self, operation, payload):
            return DocumentModelClient.messages(self, operation, payload) if kind == "document" else super().messages(operation, payload)
        async def generate(self, operation, payload, validate):
            if kind == "document" and operation == "outline":
                return validate({"content_type": CONTENT_TYPES["课程或教材"], "document_summary": "概念与应用",
                    "chapters": [{"title": "概念", "core_question": "是什么", "topics": ["应用"], "start_anchor_id": payload["anchors"][0]}]})
            return await super().generate(operation, payload, validate)
    settings = Settings(llm_key="fake-cache", note_style=style)
    model, states = Model(settings), []
    model.activity = lambda state, **details: states.append(state)
    material = {"version": 2, "units": [{"id": "D1", "label": "正文", "locator": {"block": 1}, "format": "text", "text": "说明核心概念与应用条件。"}]}
    evidence = [{"id": "speech-1", "kind": "speech", "start_seconds": 0, "end_seconds": 5, "text": "说明核心概念与应用条件。"}]
    def flow():
        if kind == "document":
            config = task_config(settings, "目录", preview(material, settings, "目录"))
            factory = ConciseDocumentGeneration if style == "concise" else DetailedDocumentGeneration
            return factory("cache", tmp_path, config, material, model, asyncio.Event(), lambda: None, lambda *a: None, lambda value: None)
        factory = ConciseVideoGeneration if style == "concise" else DetailedVideoGeneration
        return factory("cache", model, settings, tmp_path, evidence, 10, asyncio.Event(), lambda: None, lambda *a: None, {},
                       publish=lambda value: None, video_title="目录")
    first = asyncio.run(flow().run())
    assert first["chapters"]
    async def forbidden(*args, **kwargs):
        pytest.fail("a complete checkpoint must not issue a new request")
    monkeypatch.setattr(model, "generate", forbidden)
    states.clear()
    second = asyncio.run(flow().run())
    assert second["chapters"] == first["chapters"]
    assert "cached" in states
    assert not set(states) & {"pacing", "waiting", "receiving", "retry_wait", "repair"}


@pytest.mark.parametrize("model_type", [ModelClient, DocumentModelClient])
@pytest.mark.parametrize("phase", ["pacing", "waiting", "receiving", "retry_wait"])
def test_cancel_at_request_phase_clears_only_that_request(tmp_path, monkeypatch, model_type, phase):
    model = model_type(Settings(llm_key="fake-cancel", llm_stream=True, llm_request_interval_seconds=3600))
    model.diagnostics = Diagnostics(tmp_path)
    states = []
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        def record(state, **details):
            states.append(state)
            if state == phase:
                entered.set()
        model.activity = record
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        async def transport(request):
            if phase == "waiting":
                await asyncio.Event().wait()
            if phase == "retry_wait":
                return httpx.Response(503, json={"error": {"message": "temporary"}})
            return httpx.Response(200, stream=Body())
        async with model:
            await model.client.aclose()
            model.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
            if phase == "pacing":
                async def pause():
                    await asyncio.Event().wait()
                model.pacer.wait = pause
            running = asyncio.create_task(model.generate("overview", {}, model_validator("overview", lambda value: value)))
            await asyncio.wait_for(entered.wait(), 2)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not model.active_requests
            if phase == "receiving":
                assert closed.is_set()
    asyncio.run(scenario())
    assert states[-1] == "request_done" and "saved" not in states
    assert model.calls == (0 if phase == "pacing" else 1)
