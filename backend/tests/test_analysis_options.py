from dataclasses import replace
import subprocess
import sys
from types import SimpleNamespace
import pytest
from app.db import Task
from app.errors import AppError
from app.recognition.media import Recognizer
from app.recognition.local_models import MODELS, model_path
from app.videos.pipeline import Pipeline
from .conftest import upload, stored_task_config
from .fakes import FakeModel, FakeRecognizer


def prepare_models(client, settings):
    client.get("/api/v1/settings/recognition")
    for identifier in ("whisper-small", "paraformer"):
        for name in MODELS[identifier]["files"]:
            path = model_path(settings, identifier) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test model")


def test_task_model_choices_survive_default_changes_and_retry(client, app, video_file, monkeypatch):
    settings = app.state.pipeline.get_settings()
    prepare_models(client, settings)
    video = upload(client, video_file)
    selection = {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "auto", "model": "test/chosen"}
    response = client.post(f'/api/v1/videos/{video["id"]}/analyses', json=selection)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert client.get("/api/v1/settings/recognition").json()["settings"]["asr_option"] == "paraformer"
    assert client.get("/api/v1/settings/model").json()["model"] == settings.llm_model
    assert client.post(f'/api/v1/videos/{video["id"]}/analyses', json=selection).json()["reused"]
    changed = {**selection, "model": "test/other"}
    assert client.post(f'/api/v1/videos/{video["id"]}/analyses', json=changed).status_code == 409

    captured = []
    class RecordingModel(FakeModel):
        def __init__(self, current):
            captured.append(current)
            super().__init__(current)
    pipeline = Pipeline(lambda snapshot=replace(settings, llm_model="test/new-default", ocr_engine="rapidocr"): snapshot,
                        app.state.sessions, app.state.diagnostics, FakeRecognizer, RecordingModel)
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    snapshot = stored_task_config(app, task_id)
    assert snapshot["model"] == selection["model"] and snapshot["asr_language"] == selection["language"]
    assert stored_task_config(app, task["id"] )["asr_engine"] == "whisper"
    assert stored_task_config(app, task["id"] )["asr_model"] == "small"
    assert stored_task_config(app, task["id"] )["ocr_engine"] == "rapidocr"
    assert captured[0].llm_model == "test/chosen"
    assert captured[0].asr_language == "auto"
    assert pipeline.get_settings().llm_model == "test/new-default"

    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "interrupted"
    assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 202
    retried = stored_task_config(app, task_id)
    assert {k: v for k, v in retried.items() if k != "task_activity"} == {k: v for k, v in snapshot.items() if k != "task_activity"}
    assert retried["task_activity"]["attempt"] == 2
    assert retried["task_activity"]["sequence"] > snapshot["task_activity"]["sequence"]
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert captured[-1].llm_model == "test/chosen"


def test_task_selection_rejects_missing_files_and_sensitive_fields(client, app, video_file):
    video = upload(client, video_file)
    endpoint = f'/api/v1/videos/{video["id"]}/analyses'
    selection = {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "zh", "model": "test/model"}
    assert client.post(endpoint, json=selection).json()["error"]["code"] == "model_not_downloaded"
    assert client.post(endpoint, json={**selection, "token": "must-not-be-accepted"}).status_code == 422
    assert client.post(endpoint, json={**selection, "model": "   "}).status_code == 422
    prepare_models(client, app.state.pipeline.get_settings())
    assert client.post(endpoint, json={**selection, "asr_option": "paraformer", "language": "en"}).json()["error"]["code"] == "language_unsupported"
    assert client.get("/api/v1/videos").json()[0]["latest_task"] is None


def test_missing_task_model_after_submission_reports_asr_failure_and_continues_ocr(client, app, video_file, tmp_path, monkeypatch):
    settings = app.state.pipeline.get_settings()
    prepare_models(client, settings)
    source = tmp_path / "with-audio.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video_file), "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "copy", "-c:a", "aac", "-shortest", str(source)], check=True)
    video = upload(client, source)
    selection = {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "zh", "model": "test/model"}
    created = client.post(f'/api/v1/videos/{video["id"]}/analyses', json=selection)
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    folder = settings.data_dir / "videos" / video["id"]
    assert not list(folder.glob("speech-*.json")) and not list(folder.glob("*subtitle*"))
    with app.state.sessions() as session:
        from app.db import Video
        assert session.get(Video, video["id"]).audio_codec == "aac"
        assert "transcript_source" not in session.get(Task, task_id).config
    (model_path(settings, "whisper-small") / "model.bin").unlink()
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: pytest.fail("缺少权重时不得加载模型或下载")))
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args, **kwargs: pytest.fail("本任务不得获取字幕"))
    failures, diagnostics = [], []
    class MissingWeightsRecognizer(Recognizer):
        ocr_calls = 0

        def transcribe(self, path):
            assert path.is_file()
            try:
                return super().transcribe(path)
            except AppError as exc:
                failures.append(exc.code)
                raise

        def ocr(self, path):
            self.ocr_calls += 1
            return FakeRecognizer(None).ocr(path)

    pipeline = Pipeline(lambda snapshot=settings: snapshot, app.state.sessions, app.state.diagnostics, MissingWeightsRecognizer, FakeModel)
    record = pipeline.diagnostics.record
    def recording(code, message, exc, **kwargs):
        diagnostics.append((code, getattr(exc, "code", None)))
        return record(code, message, exc, **kwargs)
    monkeypatch.setattr(pipeline.diagnostics, "record", recording)
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert failures == ["model_not_downloaded"]
    assert ("asr_failed", "model_not_downloaded") in diagnostics
    assert pipeline.recognizer.ocr_calls == 60
    assert task["status"] == "succeeded" and task["error"] is None
    assert any("语音识别未完成" in warning and "画面文字" in warning for warning in task["warnings"])
    assert not list(folder.glob("speech-*.json"))
    notes = client.get(f"/api/v1/analyses/{task_id}").json()
    sources = client.get(f'/api/v1/videos/{video["id"]}/evidence?task_id={task_id}').json()
    assert sources and {item["kind"] for item in sources} == {"frame"}
    assert set(notes["evidence_ids"]) == {item["id"] for item in sources}
    assert stored_task_config(app, task_id)["asr_model"] == "small"


def test_saved_profile_is_task_only_and_queue_snapshot_is_fixed(client, app, video_file):
    settings = app.state.pipeline.get_settings()
    prepare_models(client, settings)
    created = client.post('/api/v1/settings/model/profiles', json={
        "name": "本次连接", "base_url": "https://chosen.invalid/v1", "model": "chosen-model",
        "token": "chosen-secret", "request_interval_seconds": 5, "section_concurrency": 2}).json()
    identifier = created["profiles"][-1]["id"]
    video = upload(client, video_file)
    selection = {"asr_option": "paraformer", "ocr_option": "rapidocr", "llm_profile_id": identifier}
    response = client.post(f'/api/v1/videos/{video["id"]}/analyses', json=selection)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    assert client.get('/api/v1/settings/model/profiles').json()["active_id"] == "default"
    config = stored_task_config(app, task_id)
    assert config["model"] == "chosen-model" and config["asr_language"] == "zh"
    client.put(f'/api/v1/settings/model/profiles/{identifier}', json={
        "name": "已编辑", "base_url": "https://chosen.invalid/v1", "model": "edited-model",
        "request_interval_seconds": 9, "section_concurrency": 1})
    captured = []
    class Model(FakeModel):
        def __init__(self, current):
            captured.append(current)
            super().__init__(current)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, Model)
    pipeline.run(task_id)
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "succeeded"
    assert captured[0].llm_model == "chosen-model" and captured[0].llm_key == "chosen-secret"
    assert captured[0].llm_request_interval_seconds == 5 and captured[0].llm_section_concurrency == 2
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "interrupted"
    assert client.post(f'/api/v1/tasks/{task_id}/retry').status_code == 202
    pipeline.run(task_id)
    assert captured[-1].llm_model == "chosen-model"
    public = client.get(f'/api/v1/tasks/{task_id}').text
    assert "chosen-secret" not in public and "chosen.invalid" not in public
    assert client.get('/api/v1/settings/model/profiles').json()["active_id"] == "default"


def test_profile_selection_rejects_conflicts_deleted_and_changed_endpoints(client, app, video_file):
    settings = app.state.pipeline.get_settings()
    prepare_models(client, settings)
    saved = client.post('/api/v1/settings/model/profiles', json={
        "name": "临时", "base_url": "https://chosen.invalid/v1", "model": "chosen", "token": "secret"}).json()
    identifier = saved["profiles"][-1]["id"]
    video = upload(client, video_file)
    selection = {"asr_option": "paraformer", "ocr_option": "rapidocr", "llm_profile_id": identifier}
    endpoint = f'/api/v1/videos/{video["id"]}/analyses'
    assert client.post(endpoint, json={**selection, "model": "override"}).status_code == 422
    assert client.post(endpoint, json={**selection, "llm_profile_id": "deleted"}).status_code == 404
    task_id = client.post(endpoint, json=selection).json()["task_id"]
    client.put(f'/api/v1/settings/model/profiles/{identifier}', json={
        "name": "新地址", "base_url": "https://changed.invalid/v1", "model": "chosen", "token": "new-secret"})
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer,
                        lambda *args: pytest.fail("不得把旧任务材料发往新地址"))
    pipeline.run(task_id)
    assert client.get(f'/api/v1/tasks/{task_id}').json()["error"]["code"] == "model_not_configured"
