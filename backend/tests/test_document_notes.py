import json
from unittest.mock import patch
from urllib.parse import unquote
import httpx
import pytest
from sqlalchemy import event, select
from app.db import DocumentTask, Task
from app.errors import AppError
from app.config import Settings
from app.documents.material import snapshot, split_material, preceding
from app.documents.tasks import preview
from app.documents.model import DocumentModelClient
from app.documents.prompts import CONTENT_TYPES
from app.documents.schemas import DocumentOutline
from app.documents.versions import DOCUMENT_PROMPT_VERSION
from app.videos.versions import PROMPT_VERSION
from .document_samples import sample


@pytest.fixture
def document_models(monkeypatch):
    class Calls(list):
        hook = None
    calls = Calls()
    async def respond(request):
        body = json.loads(request.content)
        for message in reversed(body["messages"]):
            if message["role"] != "user":
                continue
            try:
                payload = json.loads(message["content"])
                break
            except ValueError:
                continue
        calls.append((request, payload))
        if calls.hook:
            response = calls.hook(request, payload)
            if response is not None:
                return response
        if "anchors" in payload:
            anchors = payload["anchors"]
            points = [anchors[0], anchors[len(anchors) // 2]] if len(anchors) > 1 else [anchors[0]]
            value = {"chapters": [{"title": f"主题{i + 1}", "core_question": "本章说明什么？", "topics": ["核心解释"],
                                    "start_anchor_id": anchor} for i, anchor in enumerate(points)],
                     "document_summary": "文档阐述了相关概念和条件。", "content_type": payload.get("content_type", "课程或教材")}
        elif "chapters" in payload:
            content = "\n\n".join(f"<!-- chapter:{c['chapter_id']} -->\n<!-- summary:本章介绍必要条件。 -->\n**核心解释**，保留必要条件。"
                                    for c in payload["chapters"])
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        elif "chapter" in payload:
            value = {"facts": [{"text": "本章解释原文中的主要概念、条件和关键区别。"}], "issues": []}
        elif "repairs" in payload:
            value = {"repairs": [{**{k: r[k] for k in ("chapter_id", "block_index")}, "quote": payload["sources"][0]["text"][:10]}
                                  for r in payload["repairs"]]}
        else:
            value = {"title": "文档主题总结", "tags": ["概念"], "overview_paragraphs": ["资料解释了主要概念与必要条件。"]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    return calls


def upload(client, extension="txt", raw=None):
    response = client.post("/api/v1/documents", files={"file": (f"材料.{extension}", sample(extension) if raw is None else raw)})
    assert response.status_code == 201, response.text
    return response.json()


def start(client, doc):
    client.put("/api/v1/settings/model", json={"token": "fake-document-secret", "stream": False})
    result = client.post(f'/api/v1/documents/{doc["id"]}/analyses')
    assert result.status_code == 202, result.text
    return result.json()["task_id"]


def test_regenerate_reuses_extraction_and_keeps_other_material_notes(client, app, document_models, monkeypatch):
    monkeypatch.setattr("app.documents.store.runtime_ready", lambda _: True)
    monkeypatch.setattr("app.documents.store.model_ready", lambda *_: True)
    raw = sample("pdf")
    def imported(content):
        response = client.post("/api/v1/documents", data={"ocr_option": "rapidocr"}, files={"file": ("lesson.pdf", content)})
        assert response.status_code == 201, response.text
        return response.json()
    first = imported(raw)
    task_id = start(client, first)
    app.state.document_pipeline.run(task_id)
    notes = client.get(f"/api/v1/document-analyses/{task_id}").json()
    assert notes["complete"]
    from .pdf_samples import text_pdf
    second = imported(text_pdf(("A different document", "Second page evidence")))
    assert first["id"] != second["id"]
    assert client.get(f"/api/v1/document-analyses/{task_id}").json() == notes
    saved = (app.state.document_analyses.documents.folder(first["id"]) / "content.json").read_bytes()
    monkeypatch.setattr("app.documents.store.extract", lambda *_: pytest.fail("regeneration must reuse material"))
    regenerated = start(client, first)
    app.state.document_pipeline.run(regenerated)
    assert client.get(f"/api/v1/document-analyses/{regenerated}").json()["complete"]
    assert (app.state.document_analyses.documents.folder(first["id"]) / "content.json").read_bytes() == saved


def test_profile_budget_preview_and_queued_document_requests_use_snapshot(client, app, document_models):
    from app.analysis.budgets import OUTPUT_TOKENS
    tokens = {**OUTPUT_TOKENS, "outline": 4500, "chapters": 5500, "overview": 6500}
    response = client.post("/api/v1/settings/model/profiles", json={"name": "小范围", "token": "fake-budget",
        "input_text_limit": 6000, "output_tokens": tokens, "stream": False})
    assert response.status_code == 201
    profile = response.json()["profiles"][-1]["id"]
    doc = upload(client, raw=("材料原文。" * 1000).encode())
    uri = f'/api/v1/documents/{doc["id"]}'
    normal = client.get(uri + "/analysis-preview").json()
    selected = client.get(uri + "/analysis-preview", params={"llm_profile_id": profile}).json()
    assert selected["estimated_segments"] > normal["estimated_segments"] == 1
    assert client.get(uri + "/analysis-preview?llm_profile_id=missing").status_code == 404
    task_id = client.post(uri + "/analyses", json={"llm_profile_id": profile}).json()["task_id"]
    assert client.put(f"/api/v1/settings/model/profiles/{profile}", json={"name": "改大范围", "input_text_limit": 25000, "output_tokens": OUTPUT_TOKENS}).status_code == 200
    app.state.document_pipeline.run(task_id)
    assert client.get(f"/api/v1/document-tasks/{task_id}").json()["status"] == "succeeded"
    for request, payload in document_models:
        operation = "outline" if "anchors" in payload else "chapters" if "chapters" in payload else "overview"
        assert json.loads(request.content)["max_tokens"] == tokens[operation]
    with app.state.sessions() as session:
        config = session.get(DocumentTask, task_id).config
        assert config["llm_input_text_limit"] == 6000
        assert config["total_segments"] == selected["estimated_segments"] and config["llm_output_tokens"] == tokens


@pytest.mark.parametrize("extension", ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"])
def test_document_notes_end_to_end_without_recognizers(client, app, document_models, extension):
    doc = upload(client, extension)
    info = client.get(f'/api/v1/documents/{doc["id"]}/analysis-preview').json()
    assert info["eligible"] and not document_models
    identifier = start(client, doc)
    app.state.document_pipeline.run(identifier)
    task = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert task["status"] == "succeeded", task
    assert task["notes_state"] == "ready" and task["request_count"] >= 3
    assert "result" not in task and "fake-document-secret" not in json.dumps(task)
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert notes["complete"] and notes["format_version"] == 1
    assert "start_seconds" not in json.dumps(notes)
    sources = list(dict.fromkeys(i for c in notes["chapters"] for i in c["source_ids"]))
    response = client.get(f'/api/v1/documents/{doc["id"]}/content', params={"offset": 0, "limit": 100})
    assert response.status_code == 200 and [u["id"] for u in response.json()["units"]] == sources
    for format in ("txt", "md"):
        text = client.get(f"/api/v1/document-analyses/{identifier}/notes?format={format}").text
        assert "文档主题总结" in text and "来源：" in text and "fake-document-secret" not in text
    listing = client.get("/api/v1/documents").json()[0]
    assert listing["latest_task"]["id"] == identifier
    folder = app.state.pipeline.get_settings().data_dir / "documents" / doc["id"]
    assert not list(folder.glob(f"*-{identifier}-*.json"))
    assert (folder / "content.json").is_file()
    assert all("视频" not in request[0].content.decode() for request in document_models)


@pytest.mark.parametrize("filename, stem", [("第5章 循环结构.pptx", "第5章 循环结构"), ("课程.v2.txt", "课程.v2")])
def test_download_filename_uses_original_material_name(client, app, document_models, filename, stem):
    extension = filename.rsplit(".", 1)[-1]
    response = client.post("/api/v1/documents", files={"file": (filename, sample(extension))})
    assert response.status_code == 201
    identifier = start(client, response.json())
    app.state.document_pipeline.run(identifier)
    for format in ("txt", "md"):
        response = client.get(f"/api/v1/document-analyses/{identifier}/notes?format={format}")
        assert response.status_code == 200
        assert unquote(response.headers["Content-Disposition"]) == f"attachment; filename*=UTF-8''{stem} 笔记总结.{format}"


def test_formula_material_preserves_escaped_source_not_computed(client, app, document_models):
    doc = upload(client, "xlsx")
    identifier = start(client, doc)
    app.state.document_pipeline.run(identifier)
    payload = next(p for _, p in document_models if "evidence" in p)
    assert any(r"=B2\*2" in e["text"] for e in payload["evidence"])
    assert "不执行" in document_models[0][0].content.decode()


def test_preview_across_pages_isolation_and_resume_snapshot_guard(client, app):
    doc = upload(client, raw="\n".join(f"第{i}行" for i in range(2300)).encode())
    identifier = start(client, doc)
    query = f'/api/v1/documents/{doc["id"]}/content'
    for offset, expected in [(0, "D1"), (11, "D12"), (22, "D23")]:
        result = client.get(query, params={"offset": offset, "limit": 1})
        assert [u["id"] for u in result.json()["units"]] == [expected]
    other = upload(client, raw=b"different")
    assert client.get(f'/api/v1/documents/{other["id"]}/content').json()["units"][0]["text"] == "different"
    assert "/api/v1/documents/{identifier}/sources" not in app.openapi()["paths"]
    path = app.state.pipeline.get_settings().data_dir / "documents" / doc["id"] / "content.json"
    value = json.loads(path.read_text("utf-8"))
    value["units"][0]["text"] += "changed"
    path.write_text(json.dumps(value), "utf-8")
    client.post(f"/api/v1/document-tasks/{identifier}/stop")
    assert client.post(f"/api/v1/document-tasks/{identifier}/resume").status_code == 409


@pytest.mark.parametrize("length,eligible", [(100000, True), (100001, False)])
def test_generation_character_limits_do_not_limit_preview(client, app, length, eligible):
    doc = upload(client, raw=("字" * length).encode())
    result = client.get(f'/api/v1/documents/{doc["id"]}/analysis-preview').json()
    assert result["eligible"] == eligible and result["character_count"] == length
    assert client.get(f'/api/v1/documents/{doc["id"]}/content').status_code == 200
    if not eligible:
        assert client.post(f'/api/v1/documents/{doc["id"]}/analyses').status_code == 400
        with app.state.sessions() as session:
            assert not list(session.scalars(select(DocumentTask)))


def test_fragments_have_exact_coverage_and_context(tmp_path):
    text = ("第一段\n" * 10000) + "末尾"
    material = {"version": 1, "units": [{"id": "D1", "text": text, "label": "第1页", "locator": {"page": 1}, "format": "text"}]}
    fragments, segments = split_material(material)
    assert "".join(f["text"] for f in fragments) == text
    assert fragments[0]["start_offset"] == 0 and fragments[-1]["end_offset"] == len(text)
    assert all(a["end_offset"] == b["start_offset"] for a, b in zip(fragments, fragments[1:]))
    assert sum(len(c["text"]) for c in preceding(fragments, 1)) == 1000
    assert len(segments) > 1
    assert snapshot(material) != snapshot({**material, "units": [{**material["units"][0], "locator": {"page": 2}}]})


def test_active_task_dedup_freezing_and_delete_guard(client, app):
    doc = upload(client)
    identifier = start(client, doc)
    assert client.post(f'/api/v1/documents/{doc["id"]}/analyses').json()["task_id"] == identifier
    assert client.delete(f'/api/v1/documents/{doc["id"]}').status_code == 409
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "后续任务偏好"})
    assert client.post(f'/api/v1/documents/{doc["id"]}/analyses').status_code == 409
    assert client.post(f"/api/v1/document-tasks/{identifier}/stop").json()["status"] == "paused"
    assert client.post(f"/api/v1/document-tasks/{identifier}/resume").status_code == 202
    with app.state.sessions() as session:
        config = session.get(DocumentTask, identifier).config
        assert config["summary_prompt"] == "" and "fake-document-secret" not in json.dumps(config)
    client.post(f"/api/v1/document-tasks/{identifier}/stop")
    assert client.delete(f'/api/v1/documents/{doc["id"]}').status_code == 200
    assert client.get(f"/api/v1/document-tasks/{identifier}").status_code == 404


def test_three_successes_retention_failure_and_transaction_rollback(client, app, document_models):
    doc = upload(client)
    ids = []
    for _ in range(3):
        identifier = start(client, doc)
        app.state.document_pipeline.run(identifier)
        ids.append(identifier)
    versions = client.get(f'/api/v1/documents/{doc["id"]}/analyses').json()
    assert [v["id"] for v in versions] == ids[:0:-1]
    assert client.get(f"/api/v1/document-analyses/{ids[0]}").status_code == 409
    assert client.get(f"/api/v1/document-analyses/{ids[0]}/notes").status_code == 409
    identifier = start(client, doc)
    def fail(session, *_):
        if any(isinstance(t, DocumentTask) and t.status == "succeeded" for t in session.dirty):
            raise RuntimeError("simulated successful commit failure")
    event.listen(app.state.sessions.class_, "before_flush", fail)
    try:
        app.state.document_pipeline.run(identifier)
    finally:
        event.remove(app.state.sessions.class_, "before_flush", fail)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "failed"
    assert [v["id"] for v in client.get(f'/api/v1/documents/{doc["id"]}/analyses').json()] == ids[:0:-1]


def test_request_limit_is_persistent_and_prevents_network(client, app, document_models):
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        session.get(DocumentTask, identifier).request_count = 80
    app.state.document_pipeline.run(identifier)
    assert not document_models
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "failed" and status["request_count"] == 80
    assert client.post(f"/api/v1/document-tasks/{identifier}/retry").status_code == 409


def test_multi_segment_drafts_resume_and_frozen_preferences(client, app, document_models):
    doc = upload(client, raw=("长文材料。\n" * 9000).encode())
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "解释逻辑关系"})
    identifier = start(client, doc)
    pipeline = app.state.document_pipeline
    original = pipeline.publish
    def pause(identifier, value):
        original(identifier, value)
        client.post(f"/api/v1/document-tasks/{identifier}/stop")
    pipeline.publish = pause
    pipeline.run(identifier)
    partial = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert not partial["complete"] and partial["completed_segments"] == 1
    assert partial["covered_until_unit"].startswith("D") and partial["covered_until_offset"] > 0
    first_calls = len(document_models)
    pipeline.publish = original
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "新偏好"})
    assert client.post(f"/api/v1/document-tasks/{identifier}/resume").status_code == 202
    pipeline.run(identifier)
    final = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert final["complete"] and final["chapters"][:len(partial["chapters"])] == partial["chapters"]
    assert all("解释逻辑关系" in r.content.decode() and "新偏好" not in r.content.decode() for r, _ in document_models[first_calls:])
    assert sum(1 for _, p in document_models if p.get("anchors", [None])[0] == "F1") == 1


def test_document_model_never_calls_video_input_processing():
    from app.config import Settings
    model = DocumentModelClient(Settings(summary_prompt="用户偏好"))
    with patch("app.videos.model.deduplicate_ocr", side_effect=AssertionError("must not inspect kind")):
        for operation in model.schemas:
            messages = model.messages(operation, {"evidence": [{"id": "F1", "text": "材料"}]})
            assert messages[1]["content"].endswith("用户偏好")
            assert "video_summary" not in messages[0]["content"]
    assert DOCUMENT_PROMPT_VERSION != PROMPT_VERSION


def test_document_generation_key_tracks_current_style_budget_and_prompt():
    from app.documents.tasks import task_config
    from app.documents.versions import generation_key, check_generation
    info = {"source": {"units_digest": "sample"}, "estimated_segments": 1}
    current = task_config(Settings(), "材料", info)
    assert current["generation_key"] == generation_key(current)
    check_generation(current)
    for change in ({"note_style": "concise"}, {"llm_input_text_limit": 12000}, {"prompt_version": "old"}):
        assert generation_key({**current, **change}) != current["generation_key"]
    assert "outline_budget" not in current and "input_budget" not in current


def test_missing_document_flow_cannot_be_reused_or_resumed(client, app):
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.config = {k: v for k, v in task.config.items() if k != "generation_flow"}
    assert client.post(f'/api/v1/documents/{doc["id"]}/analyses').status_code == 409
    assert client.post(f"/api/v1/document-tasks/{identifier}/stop").status_code == 202
    assert client.post(f"/api/v1/document-tasks/{identifier}/resume").status_code == 409


@pytest.mark.parametrize("action,status", [("resume", "paused"), ("retry", "failed")])
def test_unknown_document_flow_rejected_by_control(client, app, action, status):
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.status = status
        task.config = {**task.config, "generation_flow": "future-flow"}
    response = client.post(f"/api/v1/document-tasks/{identifier}/{action}")
    assert response.status_code == 409
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == status


def test_queued_unknown_flow_fails_before_model_and_successful_history_remains_readable(client, app, document_models):
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.config = {**task.config, "generation_flow": "future-flow"}
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "failed" and status["error"]["code"] == "document_generation_changed"
    assert not document_models and status["request_count"] == 0
    from app.documents.material import load_material
    from app.documents.versions import FORMAT_VERSION
    unit = load_material(app.state.document_pipeline.get_settings().data_dir / "documents" / doc["id"])["units"][0]
    markdown = "历史笔记正文：保留概念的适用条件。"
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.status = "succeeded"
        task.result = {"format_version": FORMAT_VERSION, "kind": "document", "note_style": "detailed", "complete": True,
            "content_type": CONTENT_TYPES["课程或教材"], "title": "保留的成功笔记", "tags": [], "overview_paragraphs": [],
            "chapters": [{"chapter_id": "C1", "title": "概念", "summary": "概念及其适用条件。", "markdown": markdown, "source_ids": [unit["id"]],
                          "source_spans": [{"unit_id": unit["id"], "start_offset": 0, "end_offset": len(unit["text"])}],
                          "source_labels": [unit["label"]]}], "source_issues": [], "segment_guides": [],
            "completed_segments": 1, "total_segments": 1, "covered_until_unit": unit["id"], "covered_until_offset": len(unit["text"]),
            "source": task.config["source"], "model": task.config["model"], "prompt_version": "obsolete-prompt"}
    response = client.get(f"/api/v1/document-analyses/{identifier}")
    assert response.status_code == 200
    result = response.json()
    assert (result["id"], result["document_id"], result["prompt_version"]) == (identifier, doc["id"], "obsolete-prompt")
    assert [(c["chapter_id"], c["markdown"]) for c in result["chapters"]] == [("C1", markdown)]
    assert not document_models


def test_segment_limit_and_blank_material_preflight():
    material = {"version": 1, "units": [{"id": f"D{i + 1}", "text": "字" * 2000, "label": str(i), "locator": {"page": i + 1}, "format": "text"}
                                      for i in range(12)]}
    info = preview(material, Settings(note_style="concise", llm_input_text_limit=4500))
    assert info["estimated_segments"] > 8 and not info["eligible"]
    assert not preview({"version": 1, "units": [{**material["units"][0], "text": " \n"}]}, Settings())["eligible"]


def test_oversize_single_source_generates_without_losing_offsets(client, app, document_models):
    doc = upload(client, raw=("连续原文" * 19000).encode())
    identifier = start(client, doc)
    app.state.document_pipeline.run(identifier)
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert notes["complete"], notes
    spans = [s for c in notes["chapters"] for s in c["source_spans"]]
    assert spans[0]["start_offset"] == 0 and spans[-1]["end_offset"] == 76000
    assert all(a["end_offset"] == b["start_offset"] for a, b in zip(spans, spans[1:]))


def test_concurrent_reservations_cannot_exceed_task_limit(client, app):
    from concurrent.futures import ThreadPoolExecutor
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.status, task.request_count = "running", 79
    def reserve(_):
        try:
            app.state.document_pipeline.reserve_request(identifier)
            return True
        except AppError as exc:
            assert exc.code == "document_request_limit"
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(8))) == 1
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["request_count"] == 80


def test_transport_retries_are_charged_and_do_not_reset(client, app, document_models, monkeypatch):
    async def no_delay(_):
        pass
    monkeypatch.setattr("app.analysis.llm.asyncio.sleep", no_delay)
    def transient(_request, _payload):
        if len(document_models) <= 2:
            return httpx.Response(503, json={"error": {"message": "test unavailable"}})
    document_models.hook = transient
    doc = upload(client)
    identifier = start(client, doc)
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "succeeded"
    assert status["request_count"] == len(document_models) == 5


def test_split_batches_recover_without_repeating_truncated_request(client, app, document_models):
    doc = upload(client, "pdf")
    identifier = start(client, doc)
    paused = False
    def truncated(_request, payload):
        nonlocal paused
        if "chapters" in payload and len(payload["chapters"]) > 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]})
        if "chapters" in payload and not paused:
            paused = True
            client.post(f"/api/v1/document-tasks/{identifier}/stop")
    document_models.hook = truncated
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "paused"
    assert client.post(f"/api/v1/document-tasks/{identifier}/resume").status_code == 202
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "succeeded"
    assert sum(1 for _, p in document_models if len(p.get("chapters", [])) > 1) == 1
    assert sum(1 for _, p in document_models if "anchors" in p) == 1
    assert all(p["content_type"] == CONTENT_TYPES["课程或教材"] for _, p in document_models if "anchors" not in p)


def test_cleanup_failure_preserves_success_and_retry_does_not_change_completion(client, app, document_models, monkeypatch):
    doc = upload(client)
    identifier = start(client, doc)
    with monkeypatch.context() as context:
        context.setattr("app.analysis.checkpoints.clear_checkpoints", lambda *args: (_ for _ in ()).throw(OSError("busy file")))
        app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "succeeded"
    with app.state.sessions() as session:
        assert session.get(DocumentTask, identifier).config["checkpoint_cleanup_pending"]
    app.state.document_pipeline.retry_checkpoint_cleanup()
    with app.state.sessions() as session:
        task = session.get(DocumentTask, identifier)
        assert "checkpoint_cleanup_pending" not in task.config and task.completed_at == status["completed_at"]


def test_selected_profile_and_preference_are_frozen_without_key_leak(client, app, document_models):
    doc = upload(client)
    client.put("/api/v1/settings/model", json={"token": "default-secret", "stream": False})
    created = client.post("/api/v1/settings/model/profiles", json={"name": "文档连接", "base_url": "https://document.invalid/v1",
                      "model": "document-model", "token": "document-secret", "stream": False}).json()
    profile = created["profiles"][-1]["id"]
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "按概念比较"})
    identifier = client.post(f'/api/v1/documents/{doc["id"]}/analyses', json={"llm_profile_id": profile}).json()["task_id"]
    client.put("/api/v1/settings/model", json={"model": "changed-default"})
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "改变后的偏好"})
    app.state.document_pipeline.run(identifier)
    assert all(str(r.url).startswith("https://document.invalid/") and r.headers["authorization"] == "Bearer document-secret" for r, _ in document_models)
    assert all("按概念比较" in r.content.decode() for r, _ in document_models)
    response = client.get(f"/api/v1/document-tasks/{identifier}")
    assert "document-secret" not in response.text and "document.invalid" not in response.text


@pytest.mark.parametrize('same_time', [False, True])
def test_shared_worker_dispatches_both_kinds_in_order_and_recovers_each(client, app, video_file, monkeypatch, same_time):
    import threading
    from app.analysis.worker import Worker
    from .conftest import upload as upload_video
    video = upload_video(client, video_file)
    docs = [upload(client, raw=f"doc{i}".encode()) for i in range(4)]
    with app.state.sessions.begin() as session:
        session.add_all([DocumentTask(id="doc-first", document_id=docs[0]["id"], config={}, created_at="1", updated_at="1"),
                         Task(id="video-middle", video_id=video["id"], config={}, created_at="2", updated_at="1" if same_time else "2"),
                         DocumentTask(id="doc-last", document_id=docs[1]["id"], config={}, created_at="3", updated_at="1" if same_time else "3"),
                         DocumentTask(id="doc-running", document_id=docs[2]["id"], config={}, status="running"),
                         DocumentTask(id="doc-stopping", document_id=docs[3]["id"], config={}, status="stopping")])
    calls, done = [], threading.Event()
    def run(table, identifier):
        calls.append(identifier)
        with app.state.sessions.begin() as session:
            session.get(table, identifier).status = "succeeded"
        if len(calls) == 3:
            done.set()
    monkeypatch.setattr(app.state.pipeline, "run", lambda identifier, shutdown=None: run(Task, identifier))
    monkeypatch.setattr(app.state.document_pipeline, "run", lambda identifier, shutdown=None: run(DocumentTask, identifier))
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, app.state.pipeline)})
    worker.register("document", DocumentTask, app.state.document_pipeline)
    worker.start()
    try:
        assert done.wait(5)
        assert calls == (["doc-first", "doc-last", "video-middle"] if same_time else ["doc-first", "video-middle", "doc-last"])
        assert client.get("/api/v1/document-tasks/doc-running").json()["status"] == "interrupted"
        assert client.get("/api/v1/document-tasks/doc-stopping").json()["status"] == "paused"
    finally:
        worker.stop()


def test_queued_resume_moves_to_tail_and_older_active_task_is_visible(client, app):
    doc = upload(client)
    first = start(client, doc)
    client.post(f"/api/v1/document-tasks/{first}/stop")
    second = start(client, doc)
    client.post(f"/api/v1/document-tasks/{second}/stop")
    other = start(client, upload(client, raw=b"other document"))
    assert client.post(f"/api/v1/document-tasks/{first}/resume").status_code == 202
    listing = client.get("/api/v1/documents").json()
    assert next(d for d in listing if d["id"] == doc["id"])["latest_task"]["id"] == first
    pending = list(app.state.worker.pending.queue)
    assert pending.count(("document", first)) == 1 and pending[-1] == ("document", first)
    assert pending.index(("document", other)) < pending.index(("document", first))


def test_outline_boundary_repair_is_counted_and_keeps_contiguous_sources(client, app, document_models):
    doc = upload(client, "pdf")
    identifier = start(client, doc)
    def wrong_first(_request, payload):
        if "anchors" in payload and len(document_models) == 1:
            value = {"content_type": "课程或教材", "document_summary": "主题", "chapters": [{"title": "错位", "core_question": "问题", "topics": ["要点"],
                     "start_anchor_id": payload["anchors"][-1]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    document_models.hook = wrong_first
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "succeeded" and status["request_count"] == len(document_models) == 4
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert [i for c in notes["chapters"] for i in c["source_ids"]] == ["D1", "D2"]


def test_request_ceiling_applies_to_structure_repair(client, app, document_models):
    doc = upload(client)
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        session.get(DocumentTask, identifier).request_count = 79
    document_models.hook = lambda *_: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "failed" and status["request_count"] == 80 and len(document_models) == 1
    assert status["error"]["code"] == "document_request_limit"


def test_nonconverging_extraction_fails_and_keeps_checkpoints(client, app, document_models):
    doc = upload(client, raw=("原文材料。" * 2000).encode())
    identifier = start(client, doc)
    with app.state.sessions.begin() as session:
        task = session.get(DocumentTask, identifier)
        task.config = {**task.config, "llm_input_text_limit": 6000}
    def oversized_facts(_request, payload):
        if "anchors" in payload:
            value = {"content_type": "课程或教材", "document_summary": "主题导览。" * 600,
                     "chapters": [{"title": "概念", "core_question": "条件", "topics": ["过程"], "start_anchor_id": payload["anchors"][0]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
        if "chapter" in payload:
            value = {"facts": [{"text": "无法压缩" * 2000}], "issues": []}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    document_models.hook = oversized_facts
    app.state.document_pipeline.run(identifier)
    status = client.get(f"/api/v1/document-tasks/{identifier}").json()
    assert status["status"] == "failed" and status["error"]["code"] == "model_input_budget"
    folder = app.state.pipeline.get_settings().data_dir / "documents" / doc["id"]
    assert list(folder.glob(f"*-{identifier}-*.json"))


def test_issue_quote_is_repaired_from_exact_original_and_maps_to_document_source(client, app, document_models):
    doc = upload(client, raw="原文保留的条件与存疑内容。".encode())
    identifier = start(client, doc)
    def issue(_request, payload):
        if "chapters" in payload:
            chapter = payload["chapters"][0]
            raw = {"quote": "错误的改写引文", "reason": "条件含义存疑", "evidence_ids": [chapter["primary"][0]["id"]], "critical": True}
            content = f'<!-- chapter:{chapter["chapter_id"]} -->\n<!-- summary:保留条件。 -->\n正文。\n<!-- issue:{json.dumps(raw, ensure_ascii=False)} -->'
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    document_models.hook = issue
    app.state.document_pipeline.run(identifier)
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert notes["complete"], notes
    assert notes["source_issues"][0]["source_ids"] == ["D1"]
    assert notes["source_issues"][0]["quote"] in "原文保留的条件与存疑内容。"
    assert any("repairs" in payload for _, payload in document_models)
    assert all(payload["content_type"] == CONTENT_TYPES["课程或教材"] for _, payload in document_models if "repairs" in payload)


def test_simultaneous_creates_reuse_one_task_and_deletion_is_guarded(client, app):
    from concurrent.futures import ThreadPoolExecutor
    doc = upload(client)
    start(client, doc)
    url = f'/api/v1/documents/{doc["id"]}'
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(url + "/analyses"), range(8)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["task_id"] for r in responses}) == 1
    assert client.delete(url).status_code == 409
    assert client.get(url + "/content").status_code == 200


def test_missing_material_cannot_resume_old_checkpoint(client, app):
    doc = upload(client)
    identifier = start(client, doc)
    client.post(f"/api/v1/document-tasks/{identifier}/stop")
    (app.state.pipeline.get_settings().data_dir / "documents" / doc["id"] / "content.json").unlink()
    response = client.post(f"/api/v1/document-tasks/{identifier}/resume")
    assert response.status_code == 409 and response.json()["error"]["code"] == "document_content_missing"


@pytest.mark.parametrize("extension", ["pdf", "xlsx"])
def test_long_pdf_and_multiple_sheets_keep_all_source_offsets(client, app, document_models, extension):
    from io import BytesIO
    stream = BytesIO()
    if extension == "pdf":
        from .pdf_samples import text_pdf
        stream.write(text_pdf(("knowledge and conditions. " * 2500,)))
    else:
        from openpyxl import Workbook
        workbook = Workbook()
        for sheet in (workbook.active, workbook.create_sheet("第二张表")):
            for index in range(160):
                sheet.append([index, "condition " * 15])
        workbook.save(stream)
        workbook.close()
    doc = upload(client, extension, stream.getvalue())
    identifier = start(client, doc)
    app.state.document_pipeline.run(identifier)
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert notes["complete"] and notes["total_segments"] > 1, notes
    folder = app.state.pipeline.get_settings().data_dir / "documents" / doc["id"]
    original = json.loads((folder / "content.json").read_text("utf-8"))["units"]
    spans = [s for chapter in notes["chapters"] for s in chapter["source_spans"]]
    for unit in original:
        ranges = [s for s in spans if s["unit_id"] == unit["id"]]
        assert ranges[0]["start_offset"] == 0 and ranges[-1]["end_offset"] == len(unit["text"])
        assert all(a["end_offset"] == b["start_offset"] for a, b in zip(ranges, ranges[1:]))
    if extension == "xlsx":
        assert any("第二张表" in label for c in notes["chapters"] for label in c["source_labels"])


def test_large_chapter_summaries_are_reduced_before_final_overview(client, app, document_models):
    doc = upload(client, "pdf")
    identifier = start(client, doc)
    def long_summaries(_request, payload):
        if "chapters" in payload:
            body = "\n\n".join(f'<!-- chapter:{c["chapter_id"]} -->\n<!-- summary:{"summary " * 2000} -->\n必要正文。'
                                  for c in payload["chapters"])
            return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})
    document_models.hook = long_summaries
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "succeeded"
    overviews = [p for _, p in document_models if "summaries" in p]
    assert len(overviews) >= 2
    from app.config import Settings
    meter = DocumentModelClient(Settings())
    assert all(meter.input_characters("overview", payload) <= 25000 for payload in overviews)


def test_content_guidance_survives_extraction_writing_and_overview(client, app, document_models):
    guide = "课程材料，重点解释概念关系并保留原文例题过程。" * 400
    doc = upload(client, raw=("课程原文。" * 5200).encode())
    identifier = start(client, doc)
    def outline(_request, payload):
        if "anchors" in payload:
            value = {"content_type": payload.get("content_type", "课程或教材"), "document_summary": guide, "chapters": [{"title": "概念与例题", "core_question": "如何理解原理？",
                     "topics": ["条件与解题过程"], "start_anchor_id": payload["anchors"][0]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    document_models.hook = outline
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "succeeded"
    extracts = [p for _, p in document_models if "chapter" in p]
    drafts = [p for _, p in document_models if "chapters" in p]
    assert extracts and drafts
    assert all(p["document_summary"] == guide for p in extracts + drafts)
    payloads = [p for _, p in document_models]
    assert "content_type" not in payloads[0]
    assert all(p["content_type"] == CONTENT_TYPES["课程或教材"] for p in payloads[1:])
    overview = next(p for _, p in document_models if "summaries" in p)
    assert all(item["title"] == guide for item in overview["summaries"])
    for request, _ in document_models:
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert "可自行判断何处需要补充概念解释、知识联系、推导或解题步骤" in prompt
        assert "不把本段未出现的内容说成整份资料缺失" in prompt
        assert "只写材料支持的信息" not in prompt and "不补充外部事实" not in prompt
    writing = next(json.loads(request.content)["messages"][0]["content"]
                   for request, payload in document_models if "chapters" in payload)
    assert "必要代码保持完整" in writing and "<!-- chapter:C1 -->" in writing
    assert "<!-- summary:本章摘要 -->" in writing and "连续逐字原文" in writing
    overview_prompt = next(json.loads(request.content)["messages"][0]["content"]
                           for request, payload in document_models if "summaries" in payload)
    assert "合计2–4句话" in overview_prompt and "本阶段只写材料导览" in overview_prompt
    assert "不罗列公式、推导、代码、例题过程或技术细节" in overview_prompt
    assert "直接回答资料的核心问题" not in overview_prompt


def test_detailed_document_saves_explanation_and_corrected_code_without_quote_repair(client, app, document_models):
    doc = upload(client, raw="累乘求最小 n：1×2×…×n >= 100000。输出最后一个乘数。".encode())
    identifier = start(client, doc)
    explanation = "乘积从 1 开始，因为 1 是乘法的单位元；退出时 i 已指向下一个乘数，因此输出 i-1。"
    code = "i = 1\nr = 1\nwhile r < 100000:\n    r *= i\n    i += 1\nprint(i - 1)"
    markdown = f"### 累乘与终止条件\n\n{explanation}\n\n```python\n{code}\n```"
    def respond(_request, payload):
        if "chapters" in payload:
            content = "\n\n".join(f"<!-- chapter:{c['chapter_id']} -->\n<!-- summary:理解累乘的初值与退出条件。 -->\n{markdown}"
                                    for c in payload["chapters"])
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
    document_models.hook = respond
    app.state.document_pipeline.run(identifier)
    notes = client.get(f"/api/v1/document-analyses/{identifier}").json()
    assert notes["complete"] and notes["source_issues"] == []
    assert len(document_models) == 3 and not any("repairs" in payload for _, payload in document_models)
    chapter = notes["chapters"][0]
    assert chapter["markdown"] == markdown and chapter["source_ids"] == ["D1"]
    exported = client.get(f"/api/v1/document-analyses/{identifier}/notes?format=md").text
    assert explanation in exported and code in exported and "来源：" in exported


@pytest.mark.parametrize("content_type", CONTENT_TYPES)
@pytest.mark.parametrize("name_only", [True, False])
def test_selected_document_type_is_forwarded_to_all_later_stages(client, app, document_models, content_type, name_only):
    doc = upload(client)
    identifier = start(client, doc)
    def classify(_request, payload):
        if "anchors" in payload:
            value = {"content_type": content_type if name_only else CONTENT_TYPES[content_type], "document_summary": "资料主线与整理重点。", "chapters": [
                {"title": "主要内容", "core_question": "材料说明什么？", "topics": ["要点"], "start_anchor_id": payload["anchors"][0]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    document_models.hook = classify
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "succeeded"
    assert "content_type" not in document_models[0][1]
    assert all(payload["content_type"] == CONTENT_TYPES[content_type] for _, payload in document_models[1:])
    assert client.get(f"/api/v1/document-analyses/{identifier}").json()["content_type"] == CONTENT_TYPES[content_type]


def test_continuing_outline_cannot_replace_initial_document_type(client, app, document_models):
    doc = upload(client, raw=("事件经过与结果。" * 1000).encode())
    client.put("/api/v1/settings/model", json={"token": "fake-type", "input_text_limit": 6000, "stream": False})
    identifier = client.post(f'/api/v1/documents/{doc["id"]}/analyses').json()["task_id"]
    changed = False
    def classify(_request, payload):
        nonlocal changed
        if "anchors" in payload:
            selected = "资讯或事件记录"
            if payload.get("content_type") and not changed:
                changed = True
                selected = "经验分享"
            value = {"content_type": selected, "document_summary": "事件经过与结果。", "chapters": [
                {"title": "事件", "core_question": "发生了什么？", "topics": ["过程"], "start_anchor_id": payload["anchors"][0]}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})
    document_models.hook = classify
    app.state.document_pipeline.run(identifier)
    assert client.get(f"/api/v1/document-tasks/{identifier}").json()["status"] == "succeeded"
    assert changed
    outlines = [(request, payload) for request, payload in document_models if "anchors" in payload]
    assert len(outlines) > 2 and "content_type" not in outlines[0][1]
    assert all(p["content_type"] == CONTENT_TYPES["资讯或事件记录"] for _, p in document_models[1:])
    assert any("content_type_must_match_planned_document_type" in request.content.decode() for request, _ in outlines)


@pytest.mark.parametrize("content_type", ["未知类型", "课程或教材：自行改写的要求", "操作教程：", ""])
def test_document_type_rejects_unknown_or_rewritten_requirements(content_type):
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="content_type_must_be_a_supported_type"):
        DocumentOutline.model_validate({"content_type": content_type, "document_summary": "主题", "chapters": [
            {"title": "主要内容", "core_question": "问题", "topics": ["要点"], "start_anchor_id": "D1"}]})
