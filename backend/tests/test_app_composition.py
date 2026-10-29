import threading
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from app.db import Task
from app.main import create_app
from .conftest import upload, stored_task_config
from .support import fake_files


def test_settings_updates_reach_health_new_tasks_resume_and_retry(client, app, video_file, monkeypatch):
    original = app.state.pipeline.get_settings()
    client.get("/api/v1/settings/recognition")
    fake_files(original, "whisper-small")
    recognition = {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "auto"}
    assert client.put("/api/v1/settings/recognition", json=recognition).status_code == 200
    assert client.put("/api/v1/settings/model", json={"token": "fake-current", "model": "test/first"}).status_code == 200
    proxy = {"enabled": True, "url": "http://localhost:7890"}
    assert client.put("/api/v1/settings/network", json=proxy).json() == proxy
    health = client.get("/api/v1/health").json()
    assert health["model_configured"]
    assert health["recognition"]["asr_model"] == "small"
    assert health["recognition"]["ocr_engine"] == "rapidocr"
    assert client.get("/api/v1/settings/model").json()["model"] == "test/first"
    assert original.llm_key == "" and original.asr_engine == "paraformer"

    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    endpoint = f"/api/v1/tasks/{task_id}"
    snapshot = stored_task_config(app, task_id)
    assert snapshot["model"] == "test/first" and snapshot["asr_model"] == "small"
    assert client.post(endpoint + "/stop").json()["status"] == "paused"
    assert client.put("/api/v1/settings/model", json={"model": "test/second", "summary_prompt": "本轮测试要求"}).status_code == 200
    assert client.post(endpoint + "/resume").status_code == 202
    resumed = stored_task_config(app, task_id)
    assert {k: v for k, v in resumed.items() if k != "task_activity"} == {k: v for k, v in snapshot.items() if k != "task_activity"}
    assert resumed["task_activity"]["attempt"] == 2
    assert resumed["task_activity"]["sequence"] > snapshot["task_activity"]["sequence"]
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "failed"
    assert client.post(endpoint + "/retry").status_code == 202
    retried = stored_task_config(app, task_id)
    assert {k: v for k, v in retried.items() if k != "task_activity"} == {k: v for k, v in snapshot.items() if k != "task_activity"}
    assert retried["task_activity"]["attempt"] == 3
    assert retried["task_activity"]["sequence"] > resumed["task_activity"]["sequence"]
    assert client.post(endpoint + "/stop").json()["status"] == "paused"
    new_task = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    current = stored_task_config(app, new_task)
    assert current["model"] == "test/second" and current["summary_prompt"] == "本轮测试要求"
    assert current["asr_model"] == "small" and current["ocr_engine"] == "rapidocr"


def test_video_list_retains_complete_latest_task(client, app, video_file):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.status, task.stage = "failed", "summarizing:1/3"
        task.error = {"code": "test_failure", "error_id": "test-error"}
        task.warnings = [{"code": "test_warning"}]
        task.timings = {"transcribing": 1.25}
        task.result = {"test": "retained"}
    detail = client.get(f"/api/v1/tasks/{task_id}").json()
    listed = client.get("/api/v1/videos").json()[0]["latest_task"]
    assert listed == detail
    assert set(listed) == {"id", "video_id", "status", "stage", "attempts", "config", "created_at", "updated_at",
                           "error", "warnings", "timings", "progress", "notes_state", "notes_revision", "segment_progress", "note_style", "activity"}
    assert listed["note_style"] == "detailed"
    assert set(listed["config"]) <= {"extract_version", "outline_preview"}
    assert "fingerprint" not in client.get("/api/v1/videos").json()[0]
    assert listed["progress"]["processed"] == 1 and listed["progress"]["total"] == 3
    assert "result" not in listed and listed["notes_state"] == "none"
    with app.state.sessions() as session:
        assert session.get(Task, task_id).result == {"test": "retained"}


@pytest.mark.parametrize("segmented", [False, True])
def test_progress_contract_has_independent_steps_stage_and_timing_labels(client, app, video_file, segmented):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        task = session.get(Task, identifier)
        task.config = {**task.config, "segmented": segmented}
        task.stage = "ocr:3/12"
    result = client.get(f"/api/v1/tasks/{identifier}").json()
    progress = result["progress"]
    assert [step["key"] for step in progress["steps"]] == (["checking", "transcribing", "ocr", "segments", "finalizing"]
        if segmented else ["checking", "transcribing", "ocr", "planning", "summarizing", "finalizing"])
    assert set(progress["stage_labels"]) == {"queued", "checking", "transcribing", "ocr", "planning", "summarizing", "segments", "finalizing", "completed"}
    assert set(progress["timing_labels"]) == {"transcribing", "ocr", "planning", "summarizing", "finalizing", "total"}
    assert progress["stage_labels"]["completed"] == "分析完成"
    assert progress["timing_labels"]["total"] == "本次总耗时"
    assert (progress["processed"], progress["total"]) == (3, 12)


@pytest.mark.parametrize("operation", ["delete", "playback", "analyze", "stop", "resume", "retry"])
def test_operations_wait_for_shared_worker_lock(client, app, video_file, monkeypatch, operation):
    video = upload(client, video_file)
    video_endpoint = f'/api/v1/videos/{video["id"]}'
    if operation in {"delete", "playback", "analyze"}:
        endpoint = video_endpoint + {"delete": "", "playback": "/playback", "analyze": "/analyses"}[operation]
    else:
        task_id = client.post(video_endpoint + "/analyses").json()["task_id"]
        with app.state.sessions.begin() as session:
            session.get(Task, task_id).status = {"stop": "queued", "resume": "paused", "retry": "failed"}[operation]
        endpoint = f"/api/v1/tasks/{task_id}/{operation}"
    # Only test request coordination; avoid starting a playback conversion thread.
    monkeypatch.setattr(app.state.playback, "prepare", lambda *args: None)
    started, completed = threading.Event(), threading.Event()
    responses, errors = [], []
    def request():
        started.set()
        try:
            responses.append(client.request("DELETE" if operation == "delete" else "POST", endpoint))
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()
    thread = threading.Thread(target=request)
    with app.state.worker.lock:
        thread.start()
        assert started.wait(5)
        blocked = not completed.wait(0.2)
    thread.join(5)
    assert blocked, "request bypassed the shared worker lock"
    assert not thread.is_alive() and not errors
    assert responses[0].status_code == (200 if operation == "delete" else 202)


def test_model_options_use_current_token_one_request_and_original_limits(client, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        models = [{"id": f"test/{number:03}"} for number in reversed(range(205))]
        return httpx.Response(200, json={"data": models + [{"id": "test/000"}, {"id": 123}, {"id": "x" * 201}]},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    assert client.get("/api/v1/settings/model/options").json()["error"]["code"] == "model_not_configured"
    assert calls == []
    assert client.put("/api/v1/settings/model", json={"token": "fake-latest"}).status_code == 200
    response = client.get("/api/v1/settings/model/options")
    assert response.status_code == 200
    assert response.json() == {"models": [f"test/{number:03}" for number in range(200)]}
    assert len(calls) == 1
    assert calls[0][1] == {"headers": {"Authorization": "Bearer fake-latest"}, "timeout": 20, "follow_redirects": False}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "missing_data", "empty"])
def test_model_options_fail_without_retry_and_keep_error_report(client, monkeypatch, failure):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        request = httpx.Request("GET", url)
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        body = {"unauthorized": {"error": {"message": "test unauthorized"}}, "missing_data": {}, "empty": {"data": []}}[failure]
        return httpx.Response(401 if failure == "unauthorized" else 200, json=body, request=request)
    monkeypatch.setattr(httpx, "get", get)
    client.put("/api/v1/settings/model", json={"token": "fake-options"})
    response = client.get("/api/v1/settings/model/options")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_list_failed"
    assert response.json()["error"]["error_id"]
    assert response.headers["x-request-id"] and len(calls) == 1


@pytest.mark.parametrize("start_worker", [True, False])
def test_lifespan_preserves_start_and_close_order(app, tmp_path, monkeypatch, start_worker):
    settings = replace(app.state.pipeline.get_settings(), data_dir=tmp_path / "lifecycle", model_dir=tmp_path / "lifecycle-models",
                       log_dir=tmp_path / "lifecycle-log")
    composed = create_app(settings, start_worker=start_worker)
    events = []
    original_start = composed.state.worker.start
    def start():
        events.append("start")
        original_start()
    monkeypatch.setattr(composed.state.worker, "start", start)
    for obj, method, label in ((composed.state.worker, "stop", "stop"), (composed.state.playback, "close", "playback"),
                               (composed.state.video_downloads, "close", "downloads"), (composed.state.diagnostics, "close", "diagnostics")):
        original = getattr(obj, method)
        def close(original=original, label=label):
            events.append(label)
            original()
        monkeypatch.setattr(obj, method, close)
    with TestClient(composed) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert events == (["start"] if start_worker else [])
    assert events == (["start", "stop"] if start_worker else []) + ["playback", "downloads", "diagnostics"]
