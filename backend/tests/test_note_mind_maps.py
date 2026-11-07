import asyncio
import json
import threading
from io import BytesIO
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app.analysis.budgets import message_characters
from app.analysis.checkpoints import MIND_MAP_CLEANUP
from app.analysis.mind_map import MindMap, MindMapModel, note_payload
from app.analysis.mind_map_labels import fonts, glyph_mask, label
from app.db import Document, DocumentTask, Task, Video
from app.documents.notes import retain_two
from app.main import create_app
from app.note_mind_maps import MindMaps
from app.settings.model_profiles import ProfileInput, edit, find, for_profile
from app.videos.notes import retain_two_notes


def store_note(app, kind, task_id, day, *, long=False, complete=True):
    parent, task, field = (Video, Task, "video_id") if kind == "video" else (Document, DocumentTask, "document_id")
    fields = {"duration": 30, "video_codec": "h264"} if kind == "video" else {"format": "txt", "unit_count": 1, "character_count": 10}
    date = f"2026-11-{day:02}"
    result = {"format_version": 4, "complete": complete, "title": f"笔记 {task_id}", "overview_paragraphs": ["概览"],
              "chapters": [{"title": "第一章", "markdown": "首章正文。" * (1000 if long else 1)},
                           {"title": "短章", "markdown": "短章正文。"},
                           {"title": "末章", "markdown": "末章正文。" * (1000 if long else 1)}]}
    with app.state.sessions.begin() as session:
        if session.get(parent, "source") is None:
            session.add(parent(id="source", fingerprint="source", filename="示例", size_bytes=10, **fields))
            session.flush()
        session.add(task(id=task_id, status="succeeded", config={}, result=result, updated_at=date,
                         **{field: "source"}, **({"completed_at": date} if kind == "document" else {})))
    folder = app.state.current_settings.read().data_dir / f"{kind}s" / "source"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "mind-maps" / f"{task_id}.png"


@pytest.fixture
def model_api(app, monkeypatch):
    current = app.state.current_settings
    profile = ProfileInput(name="导图测试配置", model="test/mind-map", base_url="https://mind-map.invalid/v1",
                           token="isolated-map-token", stream=False, input_text_limit=2400)
    saved = edit(current.read(), profile)
    current.replace(saved)
    fake = SimpleNamespace(profile_id=saved.llm_profiles[-1]["id"], calls=[], answers=[], release=threading.Event(),
                           invalid=False, topic="关系图")
    fake.release.set()

    async def response(_, request):
        fake.calls.append((request, json.loads(request.content)))
        while not fake.release.is_set():
            await asyncio.sleep(0.01)
        tree = {"topic": fake.topic, "branches": [{"title": "核心概念",
                "points": ["由来源提炼出的结论。", r"平衡关系 \(K^{⦵}=p(CO_{2})/p^{⦵}\)"]}]}
        if fake.answers:
            content = fake.answers.pop(0)
        else:
            content = "{}" if fake.invalid else json.dumps(tree, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", response)
    return fake


def start(client, endpoint, model_api, replace=False):
    response = client.post(endpoint, json={"llm_profile_id": model_api.profile_id, "replace_existing": replace})
    assert response.status_code == 202, response.text
    return response.json()


def wait_state(client, endpoint, status):
    deadline = monotonic() + 8
    while True:
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] == status:
            return state
        assert monotonic() < deadline, state
        sleep(0.03)


def test_formula_symbols_have_glyphs_and_scripts_share_base_position():
    point_style = fonts()[-1]
    node = label(r"平衡 \(G_{m}^{⦵} ⇌ CO_{2}\) 与 SO₄²⁻", point_style, 496)
    glyphs = {glyph.text: glyph for glyph in node.glyphs}
    base, lower, upper = (glyphs[char] for char in ("G", "m", "⦵"))
    assert lower.x == upper.x > base.x
    assert upper.y < base.y < lower.y
    assert glyphs["2"].y > glyphs["O"].y
    for char in "⦵⇌₄²⁻":
        font = glyphs[char].font
        assert glyph_mask(font, char) != glyph_mask(font, "\uffff"), char
    assert not any(char in glyphs for char in "_^{}\\")


def test_short_formula_wraps_together():
    point_style = fonts()[-1]
    node = label(r"较长的说明 \(CaCO_{3}\)", point_style, 220)
    glyphs = {glyph.text: glyph for glyph in node.glyphs}
    assert glyphs["明"].y < glyphs["C"].y == glyphs["O"].y < glyphs["3"].y


@pytest.mark.parametrize("invalid", [r"\(\ce{SO4^2-}\)", r"\(_{2}O\)", r"\(Ca^{2}^{+}\)",
                                     r"\ce{SO4^2-}", "$SO_{4}^{2-}$", "$$SO_{4}^{2-}$$",
                                     r"\(SO_{4}^{2-}", r"SO_{4}^{2-}\)", r"\[SO_{4}^{2-}\]"])
def test_unsupported_formula_markup_is_rejected(invalid):
    value = {"topic": "硫酸根", "branches": [{"title": "离子",
             "points": [r"\(SO_{4}^{2−}\)", "SO₄²⁻", "file_name 价格 $5 到 $10"]}]}
    assert MindMap.model_validate(value).model_dump() == value
    value["branches"][0]["points"] = [invalid]
    with pytest.raises(ValidationError, match="公式"):
        MindMap.model_validate(value)


def test_raw_chemical_formula_triggers_repair_before_rendering(app, client, model_api):
    store_note(app, "video", "first", 1)
    endpoint = "/api/v1/note-mind-maps/video/first"
    invalid = json.dumps({"topic": "化学", "branches": [{"title": "离子", "points": [r"\ce{SO4^2-}"]}]})
    model_api.answers.append(invalid)
    start(client, endpoint, model_api)
    assert wait_state(client, endpoint, "idle")["image_revision"]
    assert len(model_api.calls) == 2
    assert model_api.calls[1][1]["messages"][-2] == {"role": "assistant", "content": invalid}
    assert client.get(endpoint + "/image").headers["content-type"] == "image/png"


def test_repair_fits_when_complete_note_uses_exact_input_budget(app, client, model_api):
    store_note(app, "video", "first", 1)
    endpoint = "/api/v1/note-mind-maps/video/first"
    current = app.state.current_settings.read()
    model = MindMapModel(for_profile(current, find(current, model_api.profile_id)))
    limit = model.settings.llm_input_text_limit
    with app.state.sessions.begin() as session:
        task = session.get(Task, "first")
        original = task.result
        remaining = limit - model.input_characters("mind_map", note_payload(model, original))
        first, *other = original["chapters"]
        task.result = {**original, "chapters": [{**first, "markdown": first["markdown"] + "字" * remaining}, *other]}
    model_api.answers.append('{"topic":"缺少分支"}')
    start(client, endpoint, model_api)
    state = wait_state(client, endpoint, "idle")
    assert state["image_revision"]
    first_request, repair = [body["messages"] for _, body in model_api.calls]
    assert message_characters(first_request) == limit
    assert message_characters(repair) <= limit
    assert [message["role"] for message in repair] == ["system", "user"]
    assert "上次输出未通过校验" in repair[0]["content"]
    whole, compressed = (json.loads(messages[1]["content"]) for messages in (first_request, repair))
    assert (compressed["title"], compressed["overview"]) == (whole["title"], whole["overview"])
    assert [chapter["title"] for chapter in compressed["sections"]] == ["第一章", "短章", "末章"]
    assert [chapter["text"][:4] for chapter in compressed["sections"]] == ["首章正文", "短章正文", "末章正文"]
    assert len(compressed["sections"][0]["text"]) < len(whole["sections"][0]["text"])


@pytest.mark.parametrize("kind", ["video", "document"])
def test_selected_api_long_notes_png_and_version_identity(app, client, model_api, kind):
    image = store_note(app, kind, "first", 1, long=True)
    store_note(app, kind, "second", 2)
    endpoint = f"/api/v1/note-mind-maps/{kind}/first"
    other = f"/api/v1/note-mind-maps/{kind}/second"
    assert client.get(endpoint).json() == {"status": "idle", "image_revision": None, "error": None}
    assert client.get(endpoint + "/image").status_code == 404

    model_api.release.clear()
    assert start(client, endpoint, model_api)["status"] == "generating"
    assert start(client, endpoint, model_api, replace=True)["status"] == "generating"
    assert client.get(other).json()["image_revision"] is None
    model_api.release.set()
    ready = wait_state(client, endpoint, "idle")
    assert ready["image_revision"]
    assert len(model_api.calls) == 1
    request, body = model_api.calls[0]
    assert str(request.url) == "https://mind-map.invalid/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer isolated-map-token"
    assert body["model"] == "test/mind-map"
    assert message_characters(body["messages"]) <= 2400
    note = json.loads(body["messages"][-1]["content"])
    assert note["title"] == "笔记 first" and note["overview"] == ["概览"]
    assert [chapter["title"] for chapter in note["sections"]] == ["第一章", "短章", "末章"]
    assert [chapter["text"][:4] for chapter in note["sections"]] == ["首章正文", "短章正文", "末章正文"]
    assert note["sections"][1]["text"] == "短章正文。"

    png = client.get(endpoint + "/image")
    assert png.headers["content-type"] == "image/png"
    assert png.headers["content-disposition"].startswith("inline;")
    assert png.headers["content-disposition"].endswith(".png")
    assert png.content == image.read_bytes()
    with Image.open(BytesIO(png.content)) as decoded:
        assert decoded.format == "PNG" and decoded.width >= 1200
        decoded.verify()
    download = client.get(endpoint + "/image?download=1")
    assert download.content == png.content
    assert download.headers["content-disposition"].startswith("attachment;")
    assert start(client, endpoint, model_api) == ready
    assert len(model_api.calls) == 1
    with TestClient(create_app(app.state.current_settings.read(), start_worker=False)) as reopened:
        assert reopened.get(endpoint).json() == ready
        assert reopened.get(endpoint + "/image").content == png.content


@pytest.mark.parametrize("kind", ["video", "document"])
def test_regeneration_repair_failure_keeps_image_and_retry_replaces(app, client, model_api, kind):
    image = store_note(app, kind, "first", 1)
    endpoint = f"/api/v1/note-mind-maps/{kind}/first"
    start(client, endpoint, model_api)
    original = wait_state(client, endpoint, "idle")
    png = image.read_bytes()
    assert json.loads(model_api.calls[0][1]["messages"][-1]["content"])["sections"][-1]["text"] == "末章正文。"

    model_api.invalid = True
    assert start(client, endpoint, model_api, replace=True)["image_revision"] == original["image_revision"]
    failure = wait_state(client, endpoint, "failed")
    assert failure["error"]["code"] == "invalid_model_output"
    assert failure["image_revision"] == original["image_revision"]
    assert client.get(endpoint + "/image").content == png
    assert len(model_api.calls) == 3
    model_api.invalid = False
    model_api.topic = "更新的思维导图"
    start(client, endpoint, model_api, replace=True)
    assert wait_state(client, endpoint, "idle")["image_revision"] != original["image_revision"]
    assert image.read_bytes() != png


@pytest.mark.parametrize("kind", ["video", "document"])
def test_retirement_cleans_files_and_prevents_late_publication(app, client, model_api, monkeypatch, kind):
    image = store_note(app, kind, "first", 1)
    endpoint = f"/api/v1/note-mind-maps/{kind}/first"
    start(client, endpoint, model_api)
    wait_state(client, endpoint, "idle")
    assert image.exists()

    model_api.release.clear()
    saved = threading.Event()
    original = MindMaps.save

    def watch_save(self, *args):
        try:
            return original(self, *args)
        finally:
            saved.set()

    monkeypatch.setattr(MindMaps, "save", watch_save)
    start(client, endpoint, model_api, replace=True)
    kept = store_note(app, kind, "second", 1)
    store_note(app, kind, "third", 1)
    store_note(app, kind, "partial", 4, complete=False)
    retain = retain_two_notes if kind == "video" else retain_two
    task_table = Task if kind == "video" else DocumentTask
    pipeline = app.state.pipeline if kind == "video" else app.state.document_pipeline
    with app.state.sessions.begin() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if kind == "document":
            session.get(task_table, "first").updated_at = "2026-11-05"
        pipeline.checkpoint_cleanup.defer_mind_maps(retain(session, "source"))
    versions = client.get(f"/api/v1/{kind}s/source/analyses").json()
    assert [task["id"] for task in versions] == ["third", "second"]
    assert client.get(endpoint).status_code == 409
    assert client.get(endpoint + "/image").status_code == 409
    with app.state.sessions() as session:
        assert session.get(task_table, "first").config[MIND_MAP_CLEANUP]
    pipeline.retry_checkpoint_cleanup()
    assert not image.exists()
    model_api.release.set()
    assert saved.wait(8)
    assert not image.exists()
    with app.state.sessions() as session:
        assert MIND_MAP_CLEANUP not in session.get(task_table, "first").config

    second = f"/api/v1/note-mind-maps/{kind}/second"
    start(client, second, model_api)
    wait_state(client, second, "idle")
    assert kept.exists()
    assert client.delete(f"/api/v1/{kind}s/source").status_code == 200
    assert not kept.exists()


def test_incomplete_and_unconfigured_notes_do_not_call_model(app, client, model_api):
    store_note(app, "video", "partial", 1, complete=False)
    store_note(app, "video", "complete", 2)
    root = "/api/v1/note-mind-maps/video"
    assert client.post(root + "/partial", json={"llm_profile_id": model_api.profile_id}).status_code == 409
    assert client.post(root + "/complete", json={"llm_profile_id": "default"}).json()["error"]["code"] == "model_not_configured"
    assert client.get(root + "/missing").status_code == 404
    assert model_api.calls == []


def test_shutdown_cancels_pending_replacement_and_saved_image_can_be_regenerated(app, model_api):
    image = store_note(app, "video", "first", 1)
    endpoint = "/api/v1/note-mind-maps/video/first"
    with TestClient(app) as client:
        start(client, endpoint, model_api)
        ready = wait_state(client, endpoint, "idle")
        png = image.read_bytes()
        model_api.release.clear()
        start(client, endpoint, model_api, replace=True)
        assert wait_state(client, endpoint, "generating")["image_revision"] == ready["image_revision"]
    with TestClient(create_app(app.state.current_settings.read(), start_worker=False)) as reopened:
        assert reopened.get(endpoint).json() == ready
        assert reopened.get(endpoint + "/image").content == png
        model_api.release.set()
        model_api.topic = "重启后重新生成的图"
        start(reopened, endpoint, model_api, replace=True)
        assert wait_state(reopened, endpoint, "idle")["image_revision"] != ready["image_revision"]
