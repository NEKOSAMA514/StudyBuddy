import json
import shutil
import pytest
from sqlalchemy import select
from app.db import Task, Video
from app.videos.pipeline import Pipeline
from .conftest import upload
from .fakes import FakeRecognizer, FakeModel
from .support import completed_download


def test_import_download_reuses_save_dedup_and_existing_pipeline(client, app, video_file):
    identifier, source = completed_download(app, video_file)
    response = client.post(f"/api/v1/video-downloads/{identifier}/import")
    assert response.status_code == 201, response.text
    video = response.json()
    assert video["filename"] == source.name and video["reused"] is False
    saved = app.state.pipeline.get_settings().data_dir / "videos" / video["id"] / "original.mp4"
    assert saved.read_bytes() == source.read_bytes() == video_file.read_bytes()
    assert client.get(f'/api/v1/videos/{video["id"]}/content').content == source.read_bytes()
    with app.state.sessions() as session:
        assert session.scalar(select(Task)) is None
    again = client.post(f"/api/v1/video-downloads/{identifier}/import").json()
    assert again["id"] == video["id"] and again["reused"]
    assert len(client.get("/api/v1/videos").json()) == 1
    created = client.post(f'/api/v1/videos/{video["id"]}/analyses')
    assert created.status_code == 202
    settings = app.state.pipeline.get_settings()
    pipeline = Pipeline(lambda snapshot=settings: snapshot, app.state.sessions, app.state.diagnostics, FakeRecognizer, FakeModel)
    pipeline.run(created.json()["task_id"])
    task = client.get('/api/v1/tasks/' + created.json()["task_id"]).json()
    assert task["status"] == "succeeded", task["error"]
    assert client.get(f"/api/v1/analyses/{task['id']}").json()["title"] == "TCP 慢启动与拥塞窗口增长"
    assert source.is_file()
    client.delete(f'/api/v1/videos/{video["id"]}')
    assert not saved.exists() and source.is_file()


def test_import_download_reuses_existing_uploaded_file(client, app, video_file):
    existing = upload(client, video_file)
    identifier, _ = completed_download(app, video_file)
    imported = client.post(f"/api/v1/video-downloads/{identifier}/import").json()
    assert imported["id"] == existing["id"] and imported["reused"]
    assert len(client.get("/api/v1/videos").json()) == 1


@pytest.mark.parametrize("case,code", [("unfinished", "not_ready"), ("missing", "file_missing"),
                                      ("outside", "file_missing"), ("other_task", "file_missing"),
                                      ("format", "unsupported_extension"), ("size", "upload_limit"),
                                      ("duration", "duration_limit"), ("corrupt", "invalid_media")])
def test_import_rejects_invalid_source_and_keeps_download(client, app, video_file, case, code):
    identifier, source = completed_download(app, video_file, "running" if case == "unfinished" else "succeeded")
    state_path = source.parent / "task.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if case == "missing":
        source.unlink()
    elif case == "outside":
        state["path"] = str(video_file)
    elif case == "other_task":
        other = app.state.video_downloads.directory / "other"
        other.mkdir()
        state["path"] = str(other / source.name)
        shutil.copyfile(source, state["path"])
    elif case == "format":
        source = source.rename(source.with_suffix(".txt"))
        state["path"] = str(source)
    elif case == "size":
        app.state.pipeline.get_settings().max_upload_bytes = 1
    elif case == "duration":
        app.state.pipeline.get_settings().max_duration = 1
    elif case == "corrupt":
        source.write_bytes(b"broken media")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    response = client.post(f"/api/v1/video-downloads/{identifier}/import")
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == code
    assert client.get("/api/v1/videos").json() == []
    assert not list((app.state.pipeline.get_settings().data_dir / "videos").iterdir())
    if case != "missing":
        assert source.is_file()


def test_import_unknown_identifier_and_external_origin(client):
    assert client.post("/api/v1/video-downloads/not-a-task/import").status_code == 404
    assert client.post("/api/v1/video-downloads/" + "d" * 32 + "/import",
                       headers={"Origin": "https://foreign.invalid"}).status_code == 403


def test_youtube_import_preserves_canonical_source_without_changing_old_task(client, app, video_file):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    identifier, source = completed_download(app, video_file)
    path = source.parent / "task.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["url"] = "https://youtu.be/hh73MR7j_WM?si=private-tracking"
    path.write_text(json.dumps(state), encoding="utf-8")
    imported = client.post(f"/api/v1/video-downloads/{identifier}/import").json()
    assert imported["id"] == video["id"] and imported["reused"]
    assert imported["media_info"]["source"] == {
        "platform": "youtube", "video_id": "hh73MR7j_WM", "url": "https://www.youtube.com/watch?v=hh73MR7j_WM"}
    with app.state.sessions() as session:
        assert "video_source" not in session.get(Task, task_id).config


def test_bilibili_import_and_analysis_freeze_selected_part_without_subtitle_fetch(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    old_task = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    client.post(f"/api/v1/tasks/{old_task}/stop")
    identifier, source = completed_download(app, video_file)
    path = source.parent / "task.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    origin = {"platform": "bilibili", "video_id": "BV1UhpK6RErH", "page": 2, "cid": 456,
              "url": "https://www.bilibili.com/video/BV1UhpK6RErH?p=2"}
    state.update(url=origin["url"], metadata={"source": origin})
    path.write_text(json.dumps(state), encoding="utf-8")
    imported = client.post(f"/api/v1/video-downloads/{identifier}/import").json()
    assert imported["id"] == video["id"] and imported["media_info"]["source"] == origin
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        # 分 P 上线时创建的旧任务仍只启用 YouTube 字幕，不改变其 ASR 来源。
        task = session.get(Task, task_id)
        task.config = {**task.config, "subtitle_policy": "youtube-first-v1"}
    with app.state.sessions() as session:
        assert "video_source" not in session.get(Task, old_task).config
        assert session.get(Task, task_id).config["video_source"] == origin
    status = client.get(f"/api/v1/tasks/{task_id}").json()
    assert status["progress"]["stage_labels"]["transcribing"] != "尝试获取平台字幕"
    with app.state.sessions.begin() as session:
        row = session.get(Video, video["id"])
        row.media_info = {**row.media_info, "source": {**origin, "page": 1, "cid": 123}}
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *a: pytest.fail("本轮不启用 B 站字幕"))
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, FakeModel)
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    with app.state.sessions() as session:
        assert session.get(Task, task_id).config["video_source"] == origin
        assert session.get(Task, task_id).config["transcript_source"]["kind"] == "asr"
