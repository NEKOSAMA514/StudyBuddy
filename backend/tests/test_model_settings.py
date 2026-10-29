import json
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.errors import AppError
from app.analysis.llm import ModelClient


def test_settings_persist_and_never_return_token(client, app, tmp_path):
    secret = "fake-test-token-never-a-real-key"
    value = {"token": secret, "free_tier_confirmed": True}
    response = client.put("/api/v1/settings/model", json=value)
    assert response.status_code == 200
    assert response.json()["has_token"] is True
    assert secret not in response.text
    assert secret not in client.get("/api/v1/settings/model").text
    assert response.headers["cache-control"] == "no-store"
    assert app.state.pipeline.get_settings().llm_key == secret
    path = app.state.pipeline.get_settings().data_dir
    with TestClient(create_app(Settings(data_dir=path, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["has_token"]
        assert restarted.put("/api/v1/settings/model", json={"model": "another/model"}).json()["has_token"]
        assert restarted.put("/api/v1/settings/model", json={"clear_token": True}).json()["has_token"] is False
    with TestClient(create_app(Settings(data_dir=path, llm_key="old-env-key", static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["has_token"] is False


def test_reject_other_origins_and_hosts_without_leaking_input(client):
    response = client.put("/api/v1/settings/model", headers={"Origin": "https://evil.invalid"}, json={"token": "secret"})
    assert response.status_code == 403
    response = client.put("/api/v1/settings/model", json={"base_url": "https://user:secret@evil.invalid/v1", "token": "secret"})
    assert response.status_code == 422
    assert "secret" not in response.text


def test_connection_uses_candidate_without_saving(client, monkeypatch):
    calls = []
    async def request(self, messages, max_tokens):
        calls.append((self.settings.llm_key, max_tokens, self.settings.llm_section_concurrency, self.pacer.interval))
        return '{"ok":true}'
    monkeypatch.setattr(ModelClient, "request", request)
    response = client.post("/api/v1/settings/model/test", json={"token": "fake-probe", "free_tier_confirmed": True, "section_concurrency": 2, "request_interval_seconds": 7})
    assert response.json()["ok"] is True
    assert calls == [("fake-probe", 1024, 2, 7)]
    assert not client.get("/api/v1/settings/model").json()["has_token"]
    assert client.get("/api/v1/settings/model").json()["section_concurrency"] == 3
    assert client.get("/api/v1/settings/model").json()["request_interval_seconds"] == 3


def test_running_pipeline_retains_settings_snapshot(app):
    original = app.state.pipeline.get_settings()
    with TestClient(app) as client:
        client.put("/api/v1/settings/model", json={"token": "fake-new"})
    assert original.llm_key == ""
    assert app.state.pipeline.get_settings() is not original


def test_bad_recognition_file_stops_startup_without_overwriting_settings(client, app, tmp_path):
    from dataclasses import replace
    client.put("/api/v1/settings/model", json={"token": "fake-preserved"})
    settings = app.state.pipeline.get_settings()
    model_path = settings.data_dir / "_private" / "model-settings.json"
    saved_model = model_path.read_bytes()
    path = settings.data_dir / "_private" / "recognition-settings.json"
    path.write_text('{"asr_option":123}', encoding="utf-8")
    with pytest.raises(AppError) as error:
        create_app(replace(settings, static_dir=tmp_path / "absent"), start_worker=False)
    assert error.value.code == "settings_load_failed"
    assert error.value.__cause__ is not None
    assert model_path.read_bytes() == saved_model
    assert path.read_text(encoding="utf-8") == '{"asr_option":123}'
    reports = list(settings.log_dir.glob("*.log"))
    assert reports
    logged = "\n".join(report.read_text(encoding="utf-8") for report in reports)
    assert "asr_option" in logged and "ValidationError" in logged
    assert "fake-preserved" not in logged


def test_optional_prompt_defaults_edit_preserve_clear_and_persist(client, app, tmp_path):
    original = client.get("/api/v1/settings/model").json()
    assert original["summary_prompt"] == ""
    assert original["summary_prompt"] == original["default_summary_prompt"]
    response = client.put("/api/v1/settings/model", json={"summary_prompt": "重点解释每个概念的条件与例子。"})
    assert response.status_code == 200
    assert client.put("/api/v1/settings/model", json={"model": "test/model"}).json()["summary_prompt"] == "重点解释每个概念的条件与例子。"
    assert client.put("/api/v1/settings/model", json={"summary_prompt": "字" * 10001}).status_code == 422
    with TestClient(create_app(Settings(data_dir=app.state.pipeline.get_settings().data_dir, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        saved = restarted.get("/api/v1/settings/model").json()
        assert saved["summary_prompt"] == "重点解释每个概念的条件与例子。"
        assert saved["default_summary_prompt"] == original["default_summary_prompt"]
        assert restarted.put("/api/v1/settings/model", json={"summary_prompt": "   "}).json()["summary_prompt"] == ""
    with TestClient(create_app(Settings(data_dir=app.state.pipeline.get_settings().data_dir, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["summary_prompt"] == ""


def test_concurrency_defaults_persists_and_preserves_when_omitted(client, app, tmp_path):
    assert client.get("/api/v1/settings/model").json()["section_concurrency"] == 3
    original = app.state.pipeline.get_settings()
    assert client.put("/api/v1/settings/model", json={"section_concurrency": 2}).json()["section_concurrency"] == 2
    assert original.llm_section_concurrency == 3
    assert app.state.pipeline.get_settings().llm_section_concurrency == 2
    assert client.put("/api/v1/settings/model", json={"model": "test/model"}).json()["section_concurrency"] == 2
    with TestClient(create_app(Settings(data_dir=original.data_dir, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["section_concurrency"] == 2


@pytest.mark.parametrize("interval", [1, 7, 60])
def test_request_interval_defaults_persists_and_preserves_when_omitted(client, app, tmp_path, interval):
    assert client.get("/api/v1/settings/model").json()["request_interval_seconds"] == 3
    original = app.state.pipeline.get_settings()
    saved = client.put("/api/v1/settings/model", json={"request_interval_seconds": interval}).json()
    assert saved["request_interval_seconds"] == interval
    assert original.llm_request_interval_seconds == 3
    assert app.state.pipeline.get_settings().llm_request_interval_seconds == interval
    assert client.put("/api/v1/settings/model", json={"model": "test/updated"}).json()["request_interval_seconds"] == interval
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "原有要求"})
    with TestClient(create_app(Settings(data_dir=original.data_dir, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["request_interval_seconds"] == interval
        assert restarted.get("/api/v1/settings/note-style").json()["summary_prompt"] == "原有要求"


@pytest.mark.parametrize("value", [0, -1, 61, 1.5, "5", True])
def test_request_interval_rejects_invalid_values_without_saving(client, app, value):
    original = app.state.pipeline.get_settings()
    for path in ("/api/v1/settings/model", "/api/v1/settings/model/test"):
        method = client.post if path.endswith("/test") else client.put
        assert method(path, json={"request_interval_seconds": value}).status_code == 422
    assert app.state.pipeline.get_settings() is original
    assert not (original.data_dir / "_private/model-settings.json").exists()


def test_old_settings_without_request_interval_keep_the_initial_default(tmp_path):
    path = tmp_path / "data/_private/model-settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "test/old", "section_concurrency": 2}), encoding="utf-8")
    with TestClient(create_app(Settings(data_dir=path.parent.parent, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/model").json()["request_interval_seconds"] == 3


@pytest.mark.parametrize("value", [0, 5, -1, 1.5, "2", True])
def test_concurrency_rejects_invalid_values_without_saving(client, app, value):
    original = app.state.pipeline.get_settings()
    response = client.put("/api/v1/settings/model", json={"section_concurrency": value})
    assert response.status_code == 422
    assert app.state.pipeline.get_settings() is original
    assert not (original.data_dir / "_private/model-settings.json").exists()


def test_older_saved_settings_without_concurrency_use_environment_default(tmp_path):
    path = tmp_path / "data/_private/model-settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "test/old", "summary_prompt": "原有笔记要求"}), encoding="utf-8")
    with TestClient(create_app(Settings(data_dir=path.parent.parent, llm_section_concurrency=1,
                                      static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        saved = restarted.get("/api/v1/settings/model").json()
        assert saved["section_concurrency"] == 1 and saved["summary_prompt"] == "原有笔记要求"


def test_note_style_saves_and_clears_without_overwriting_model_settings(client, app, tmp_path):
    client.put("/api/v1/settings/model", json={"token": "fake-preserved", "model": "test/preserved",
        "stream": False, "json_mode": True, "free_tier_confirmed": True, "section_concurrency": 2})
    original = app.state.pipeline.get_settings()
    expected = client.get("/api/v1/settings/model").json()
    response = client.put("/api/v1/settings/note-style", json={"summary_prompt": "  先解释条件，再给出例子。  "})
    assert response.status_code == 200 and response.json() == {"summary_prompt": "先解释条件，再给出例子。", "note_style": "detailed"}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/settings/note-style").json() == response.json()
    assert client.get("/api/v1/settings/model").json() == {**expected, **response.json()}
    assert original.summary_prompt == "" and app.state.pipeline.get_settings().llm_key == "fake-preserved"
    assert app.state.pipeline.get_settings().summary_prompt == response.json()["summary_prompt"]
    with TestClient(create_app(Settings(data_dir=original.data_dir, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        assert restarted.get("/api/v1/settings/note-style").json() == response.json()
        assert restarted.put("/api/v1/settings/note-style", json={"summary_prompt": ""}).json() == {"summary_prompt": "", "note_style": "detailed"}
        assert restarted.get("/api/v1/settings/model").json() == expected


@pytest.mark.parametrize("body", [{}, {"summary_prompt": "字" * 10001}, {"summary_prompt": "", "model": "not allowed"}])
def test_note_style_validation_preserves_saved_settings(client, app, body):
    original = app.state.pipeline.get_settings()
    assert client.put("/api/v1/settings/note-style", json=body).status_code == 422
    assert app.state.pipeline.get_settings() is original
    assert client.put("/api/v1/settings/note-style", headers={"Origin": "https://evil.invalid"},
                      json={"summary_prompt": "blocked"}).status_code == 403


def test_saved_concurrency_and_style_apply_to_next_run_only(client, app, video_file):
    from .conftest import upload
    from .fakes import FakeModel, FakeRecognizer
    captured = []
    class Model(FakeModel):
        def __init__(self, settings):
            super().__init__(settings)
            captured.append((settings.llm_section_concurrency, settings.summary_prompt, settings.llm_request_interval_seconds))
        async def generate(self, operation, payload, validate):
            if operation == "outline" and len(captured) == 1:
                assert client.put("/api/v1/settings/model", json={"section_concurrency": 1, "request_interval_seconds": 7}).status_code == 200
                assert client.put("/api/v1/settings/note-style", json={"summary_prompt": "下一次使用"}).status_code == 200
                assert self.settings.llm_section_concurrency == 2 and self.settings.summary_prompt == ""
                assert self.settings.llm_request_interval_seconds == 3
            return await super().generate(operation, payload, validate)
    pipeline = app.state.pipeline
    pipeline.recognizer_factory, pipeline.model_factory = FakeRecognizer, Model
    assert client.put("/api/v1/settings/model", json={"section_concurrency": 2}).status_code == 200
    video = upload(client, video_file)
    for _ in range(2):
        task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
        pipeline.run(task_id)
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert captured == [(2, "", 3), (1, "下一次使用", 7)]
