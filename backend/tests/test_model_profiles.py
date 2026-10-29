import json
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.errors import AppError
from app.analysis.llm import ModelClient
from app.settings.model_profiles import execution_profile, execution_settings, task_model_config, EXECUTION_FIELDS
from app.analysis.budgets import OUTPUT_TOKENS

ROOT = "/api/v1/settings/model/profiles"


def test_execution_snapshot_is_independent_and_restores_saved_values():
    saved = Settings(llm_key="fake-original", llm_model="saved-model", summary_prompt="保存的要求",
        note_style="concise", llm_input_text_limit=13000, llm_output_tokens={**OUTPUT_TOKENS, "chapters": 12000})
    config = task_model_config(saved)
    assert "llm_key" not in config and "token" not in config
    assert config["llm_output_tokens"] == saved.llm_output_tokens
    assert config["llm_output_tokens"] is not saved.llm_output_tokens
    current = replace(saved, llm_key="fake-current", llm_model="new-model", summary_prompt="新要求",
                      note_style="detailed", llm_input_text_limit=20000, llm_output_tokens=OUTPUT_TOKENS)
    restored = execution_settings(current, config, required_fields=EXECUTION_FIELDS)
    assert task_model_config(restored) == config and restored.llm_key == "fake-current"
    assert restored.llm_output_tokens is not config["llm_output_tokens"]
    config.pop("llm_output_tokens")
    defaults = execution_settings(replace(current, llm_output_tokens={"chapters": 1}), config)
    assert defaults.llm_output_tokens == OUTPUT_TOKENS and defaults.llm_output_tokens is not OUTPUT_TOKENS


@pytest.mark.parametrize("field", EXECUTION_FIELDS)
def test_execution_fields_keep_video_optional_and_document_required_rules(field):
    saved = Settings()
    config = task_model_config(saved)
    config.pop(field)
    if field == "llm_base_url":
        config.pop("llm_profile_id")
    restored = execution_settings(saved, config)
    assert getattr(restored, field) == getattr(execution_profile(saved, config), field)
    with pytest.raises(KeyError) as error:
        execution_settings(saved, config, required_fields=EXECUTION_FIELDS)
    assert error.value.args == (field,)


def test_execution_profile_still_requires_address_before_optional_field_restore():
    config = task_model_config(Settings())
    config.pop("llm_base_url")
    with pytest.raises(KeyError) as error:
        execution_settings(Settings(), config)
    assert error.value.args == ("llm_base_url",)


@pytest.mark.parametrize("required", [(), EXECUTION_FIELDS])
def test_execution_validation_keeps_profile_fields_model_and_budget_order(required):
    saved = Settings()
    config = task_model_config(saved)
    config.pop("llm_input_text_limit")
    with pytest.raises(KeyError) as error:
        execution_settings(saved, config, required_fields=required)
    assert error.value.args == ("llm_input_text_limit",)
    config.pop("provider")
    config.pop("model")
    with pytest.raises(KeyError) as error:
        execution_settings(saved, config, required_fields=required)
    assert error.value.args == ("model",)
    config.pop("llm_stream")
    with pytest.raises(KeyError) as error:
        execution_settings(saved, config, required_fields=required)
    assert error.value.args == ("llm_stream" if required else "model",)
    config["llm_base_url"] = "https://changed.invalid/v1"
    with pytest.raises(AppError) as error:
        execution_settings(saved, config, required_fields=required)
    assert error.value.code == "model_not_configured"


def add(client, name="网关", key="fake-gateway-key", url="https://gateway.invalid/v1"):
    response = client.post(ROOT, json={"name": name, "base_url": url, "model": "gateway-model",
                                      "token": key, "free_tier_confirmed": True})
    assert response.status_code == 201, response.text
    return response.json()["profiles"][-1]["id"]


def test_migrate_legacy_without_writing_until_edit_and_preserve_all_configs(client, app, tmp_path):
    client.put("/api/v1/settings/model", json={"token": "fake-legacy", "model": "old-model", "section_concurrency": 2})
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "保留原偏好"})
    path = app.state.pipeline.get_settings().data_dir / "_private/model-settings.json"
    before = path.read_bytes()
    first = client.get(ROOT)
    assert first.json()["active_id"] == "default"
    assert first.json()["profiles"][0]["model"] == "old-model"
    assert path.read_bytes() == before
    identifier = add(client)
    assert client.get("/api/v1/settings/model").json()["model"] == "old-model"
    assert client.post(f"{ROOT}/{identifier}/activate").status_code == 200
    original = app.state.pipeline.get_settings()
    assert original.llm_key == "fake-gateway-key" and original.llm_provider == "openai-compatible"
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "新偏好"})
    with TestClient(create_app(Settings(data_dir=original.data_dir, llm_key="fake-env", static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        values = restarted.get(ROOT).json()
        assert values["active_id"] == identifier and len(values["profiles"]) == 2
        assert "fake-gateway-key" not in json.dumps(values)
        assert restarted.get("/api/v1/settings/note-style").json()["summary_prompt"] == "新偏好"
        assert restarted.post(f"{ROOT}/default/activate").status_code == 200
        assert restarted.get("/api/v1/settings/model").json()["model"] == "old-model"
        assert restarted.put("/api/v1/settings/model", json={"model": "edited-legacy"}).status_code == 200
        assert restarted.get(ROOT).json()["profiles"][0]["model"] == "edited-legacy"
    assert original.llm_key == "fake-gateway-key"


def test_profile_keys_are_isolated_in_unsaved_tests_and_address_changes(client, app, monkeypatch):
    client.put("/api/v1/settings/model", json={"token": "fake-default"})
    identifier = add(client)
    captured = []
    async def request(self, *args, **kwargs):
        captured.append((self.settings.llm_base_url, self.settings.llm_key))
        if not self.settings.llm_key:
            raise AppError("model_not_configured", "missing")
        return '{"ok":true}'
    monkeypatch.setattr(ModelClient, "request", request)
    body = {"name": "修改中", "base_url": "https://gateway.invalid/v1", "model": "unsaved", "free_tier_confirmed": True}
    assert client.post(ROOT + "/test", params={"profile_id": identifier}, json=body).status_code == 200
    body["base_url"] = "https://other.invalid/v1"
    assert client.post(ROOT + "/test", params={"profile_id": identifier}, json=body).status_code == 400
    assert client.post(ROOT + "/test", json=body).status_code == 400
    assert captured == [("https://gateway.invalid/v1", "fake-gateway-key"), ("https://other.invalid/v1", ""), ("https://other.invalid/v1", "")]
    assert app.state.pipeline.get_settings().llm_key == "fake-default"
    assert client.put(f"{ROOT}/{identifier}", json=body).status_code == 200
    assert client.post(f"{ROOT}/{identifier}/activate").status_code == 200
    assert not app.state.pipeline.get_settings().llm_key


def test_options_use_current_form_and_selected_key_without_saving(client, monkeypatch):
    identifier = add(client)
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"], kwargs["follow_redirects"]))
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": [{"id": "z"}, {"id": "a"}]})
    monkeypatch.setattr(httpx, "get", get)
    body = {"name": "网关", "base_url": "https://gateway.invalid/v1", "model": "not-saved"}
    response = client.post(ROOT + "/options", params={"profile_id": identifier}, json=body)
    assert response.json() == {"models": ["a", "z"]}
    body.update(base_url="https://unsaved.invalid/v1", token="fake-unsaved")
    assert client.post(ROOT + "/options", json=body).status_code == 200
    assert calls == [("https://gateway.invalid/v1/models", "Bearer fake-gateway-key", False),
                     ("https://unsaved.invalid/v1/models", "Bearer fake-unsaved", False)]
    assert client.get(ROOT).json()["profiles"][-1]["model"] == "gateway-model"


def test_clear_key_delete_and_restart_never_restore_env(client, app, tmp_path):
    identifier = add(client)
    assert client.delete(f"{ROOT}/default").status_code == 409
    body = {"name": "网关", "base_url": "https://gateway.invalid/v1", "model": "gateway-model", "clear_token": True}
    assert client.put(f"{ROOT}/{identifier}", json=body).status_code == 200
    assert client.post(f"{ROOT}/{identifier}/activate").status_code == 200
    assert client.delete(f"{ROOT}/default").status_code == 200
    with TestClient(create_app(Settings(data_dir=app.state.pipeline.get_settings().data_dir, llm_key="fake-env", static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        result = restarted.get(ROOT).json()
        assert len(result["profiles"]) == 1 and not result["profiles"][0]["has_token"]
        assert not restarted.get("/api/v1/settings/model").json()["has_token"]
        assert restarted.delete(f"{ROOT}/{identifier}").status_code == 409


@pytest.mark.parametrize("url", ["http://remote.invalid/v1", "https://u:secret@gateway.invalid/v1", "https://gateway.invalid/v1?key=secret", "https://gateway.invalid/v1#secret", "file:///tmp/api", "https://gateway.invalid:99999/v1"])
def test_invalid_endpoints_do_not_save_or_leak(client, url):
    before = client.get(ROOT).json()
    response = client.post(ROOT, json={"name": "bad", "base_url": url, "token": "secret"})
    assert response.status_code == 422 and "secret" not in response.text
    assert client.get(ROOT).json() == before


def test_loopback_http_and_cross_origin_protection(client):
    identifier = add(client, url="http://127.0.0.1:1234/v1")
    assert identifier
    response = client.post(ROOT, headers={"Origin": "https://evil.invalid"}, json={"token": "secret"})
    assert response.status_code == 403 and "secret" not in response.text


def test_failed_atomic_save_preserves_memory_and_disk(client, app, monkeypatch):
    from pathlib import Path
    identifier = add(client)
    current = app.state.pipeline.get_settings()
    path = current.data_dir / "_private/model-settings.json"
    before = path.read_bytes()
    def fail(*args):
        raise OSError("simulated save failure")
    monkeypatch.setattr(Path, "replace", fail)
    assert client.post(f"{ROOT}/{identifier}/activate").status_code == 500
    assert app.state.pipeline.get_settings() is current
    assert path.read_bytes() == before


def test_resume_uses_original_profile_and_refuses_changed_address(client, app):
    client.put("/api/v1/settings/model", json={"token": "fake-old", "free_tier_confirmed": True})
    old = app.state.pipeline.get_settings()
    identifier = add(client)
    client.post(f"{ROOT}/{identifier}/activate")
    current = app.state.pipeline.get_settings()
    config = {"llm_profile_id": "default", "llm_base_url": old.llm_base_url}
    resolved = execution_profile(current, config)
    assert resolved.llm_key == "fake-old" and current.llm_key == "fake-gateway-key"
    assert execution_profile(current, {"llm_base_url": old.llm_base_url}).llm_key == "fake-old"
    changed = replace(current, llm_profiles=tuple({**p, "base_url": "https://changed.invalid/v1"} if p["id"] == "default" else p for p in current.llm_profiles))
    with pytest.raises(AppError):
        execution_profile(changed, config)
    client.delete(ROOT + "/default")
    with pytest.raises(AppError):
        execution_profile(app.state.pipeline.get_settings(), config)


def test_switch_during_generation_and_retry_keep_original_connection(client, app, video_file):
    from .conftest import upload
    from .fakes import FakeModel, FakeRecognizer
    client.put("/api/v1/settings/model", json={"token": "fake-original", "free_tier_confirmed": True})
    identifier = add(client)
    captured = []
    class Model(FakeModel):
        def __init__(self, settings):
            super().__init__(settings)
            captured.append((settings.llm_base_url, settings.llm_key))

        async def generate(self, operation, payload, validate):
            if operation == "outline" and len(captured) == 1:
                client.post(f"{ROOT}/{identifier}/activate")
                assert self.settings.llm_key == "fake-original"
                raise AppError("model_unavailable", "simulated failure")
            return await super().generate(operation, payload, validate)
    pipeline = app.state.pipeline
    pipeline.model_factory, pipeline.recognizer_factory = Model, FakeRecognizer
    video = upload(client, video_file)
    task = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(task)
    assert client.get(f"/api/v1/tasks/{task}").json()["status"] == "failed"
    assert client.post(f"/api/v1/tasks/{task}/retry").status_code == 202
    pipeline.run(task)
    assert client.get(f"/api/v1/tasks/{task}").json()["status"] == "succeeded"
    next_task = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(next_task)
    assert client.get(f"/api/v1/tasks/{next_task}").json()["status"] == "succeeded"
    assert captured[0] == captured[1] and captured[1][1] == "fake-original"
    assert captured[2] == ("https://gateway.invalid/v1", "fake-gateway-key")
    from app.db import Task
    with app.state.sessions() as session:
        assert "fake-original" not in json.dumps(session.get(Task, task).config)


@pytest.mark.parametrize("confirmation", [False, True, None])
def test_candidate_test_uses_actual_chat_transport_without_saving(client, monkeypatch, confirmation):
    captured = []
    original = httpx.AsyncClient
    def respond(request):
        captured.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    body = {"name": "网关", "base_url": "https://wire.invalid/v1", "model": "wire-model", "token": "fake-wire", "stream": False}
    if confirmation is not None:
        body["free_tier_confirmed"] = confirmation
    assert client.post(ROOT + "/test", json=body).status_code == 200
    assert captured == [("https://wire.invalid/v1/chat/completions", "Bearer fake-wire")]
    assert client.get(ROOT).json()["profiles"][0]["has_token"] is False
