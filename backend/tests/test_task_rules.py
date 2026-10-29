import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from app.db import Task, Video
from app.diagnostics import error_log_location
from .conftest import upload
from .support import fake_files


@pytest.mark.parametrize("status", ["queued", "running", "stopping"])
def test_active_task_reuse_model_conflict_delete_resume_and_retry_guards(client, app, video_file, status):
    video = upload(client, video_file)
    selection = {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "auto", "model": "test/chosen"}
    client.get("/api/v1/settings/recognition")
    fake_files(app.state.pipeline.get_settings(), "whisper-small")
    assert client.put("/api/v1/settings/recognition", json={k: v for k, v in selection.items() if k != "model"}).status_code == 200
    assert client.put("/api/v1/settings/model", json={"model": selection["model"]}).status_code == 200
    created = client.post(f'/api/v1/videos/{video["id"]}/analyses', json=selection)
    assert created.status_code == 202, created.text
    active = created.json()["task_id"]
    assert app.state.worker.pending.get_nowait() == ("video", active)
    app.state.worker.pending.task_done()
    with app.state.sessions.begin() as session:
        session.get(Task, active).status = status
        session.add(Task(id="paused", video_id=video["id"], status="paused", config={}))
        session.add(Task(id="failed", video_id=video["id"], status="failed", config={}))
    before = {identifier: client.get(f"/api/v1/tasks/{identifier}").json() for identifier in (active, "paused", "failed")}
    endpoint = f'/api/v1/videos/{video["id"]}'
    expected = {"task_id": active, "status": status, "reused": True}
    assert client.post(endpoint + "/analyses").json() == expected
    assert client.post(endpoint + "/analyses", json=selection).json() == expected
    response = client.post(endpoint + "/analyses", json={**selection, "model": "test/different"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "already_running"
    assert client.delete(endpoint).json()["error"]["code"] == "video_in_use"
    for identifier, action in (("paused", "resume"), ("failed", "retry")):
        response = client.post(f"/api/v1/tasks/{identifier}/{action}")
        assert response.status_code == 409 and response.json()["error"]["code"] == "already_running"
    assert {identifier: client.get(f"/api/v1/tasks/{identifier}").json() for identifier in before} == before
    assert app.state.worker.pending.qsize() == 0


@pytest.mark.parametrize("status", ["paused", "interrupted", "failed", "succeeded"])
def test_nonactive_task_does_not_block_new_analysis(client, app, video_file, status):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.add(Task(id="previous", video_id=video["id"], status=status, config={}))
    response = client.post(f'/api/v1/videos/{video["id"]}/analyses')
    assert response.status_code == 202
    assert response.json()["reused"] is False and response.json()["task_id"] != "previous"
    assert client.get("/api/v1/tasks/previous").json()["status"] == status


def test_active_task_query_is_scoped_to_current_video(client, app, video_file):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.add(Video(id="other", fingerprint="other", filename="other.mp4", duration=2, size_bytes=1, video_codec="h264"))
        session.flush()
        session.add(Task(id="other-running", video_id="other", status="running", config={}))
    endpoint = f'/api/v1/videos/{video["id"]}'
    response = client.post(endpoint + "/analyses")
    assert response.status_code == 202 and response.json()["reused"] is False
    task_id = response.json()["task_id"]
    control = f"/api/v1/tasks/{task_id}"
    assert client.post(control + "/stop").json()["status"] == "paused"
    assert client.post(control + "/resume").status_code == 202
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "failed"
    assert client.post(control + "/retry").status_code == 202
    assert client.post(control + "/stop").json()["status"] == "paused"
    assert client.delete(endpoint).json() == {"deleted": True, "warning": None}
    assert client.get("/api/v1/tasks/other-running").json()["status"] == "running"


@pytest.mark.parametrize("saved", [True, False])
def test_recognition_log_location_keeps_saved_and_unsaved_text(saved):
    reference = {"log_saved": saved, "error_id": "test-error"}
    if saved:
        reference["log_file"] = "log/test-error.log"
    expected = "详细日志：log/test-error.log（错误编号 test-error）。" if saved else "错误日志未能写入，请检查 log 文件夹权限和磁盘空间。"
    assert error_log_location(reference) == expected


@pytest.mark.parametrize("mode", ["missing", "custom", "empty"])
def test_shared_model_defaults_preserve_environment_overrides_and_input_defaults(mode):
    defaults = ["modelscope", "https://api-inference.modelscope.cn/v1", "deepseek-ai/DeepSeek-V4.1-Flash"]
    override = ["custom-provider", "http://test.invalid/v1", "test/custom"]
    environment = dict(os.environ)
    for index, name in enumerate(("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL")):
        environment.pop(name, None)
        if mode != "missing":
            environment[name] = override[index] if mode == "custom" else ""
    code = '''
import json
from app.config import Settings
from app.settings.model_settings import ModelSettingsInput
settings, value = Settings(), ModelSettingsInput()
print(json.dumps([[settings.llm_provider, settings.llm_base_url, settings.llm_model], [value.base_url, value.model]]))
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    expected = defaults if mode == "missing" else override if mode == "custom" else ["", "", ""]
    assert json.loads(result.stdout) == [expected, defaults[1:]]
