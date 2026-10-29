import subprocess
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.config import Settings
from app.db import Task, Video
from app.main import create_app
from app.analysis.worker import Worker
from .conftest import upload


def test_upload_dedup_playback_and_restart(client, app, video_file):
    first = upload(client, video_file)
    second = upload(client, video_file)
    assert first["id"] == second["id"] and second["reused"]
    response = client.get(f'/api/v1/videos/{first["id"]}/content', headers={"Range": "bytes=0-31"})
    assert response.status_code == 206
    assert len(response.content) == 32
    assert response.headers["content-range"].startswith("bytes 0-31/")
    settings = app.state.pipeline.get_settings()
    with TestClient(create_app(settings, start_worker=False)) as restarted:
        assert restarted.get("/api/v1/videos").json()[0]["id"] == first["id"]


def test_reject_empty_corrupt_extension_and_missing(client):
    assert client.post("/api/v1/videos", files={"file": ("x.mp4", b"", "video/mp4")}).status_code == 400
    assert client.post("/api/v1/videos", files={"file": ("x.mp4", b"not a video", "video/mp4")}).status_code == 400
    assert client.post("/api/v1/videos", files={"file": ("x.exe", b"x", "video/mp4")}).status_code == 400
    assert client.get("/api/v1/videos/not-real/content").status_code == 404
    assert client.get("/api/v1/evidence/not-real/image").status_code == 404


def test_size_duration_and_codec_limits(tmp_path, video_file):
    settings = Settings(data_dir=tmp_path / "tiny", max_upload_bytes=10)
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert client.post("/api/v1/videos", files={"file": ("x.mp4", video_file.read_bytes())}).status_code == 413
    settings = Settings(data_dir=tmp_path / "short", max_duration=1)
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert client.post("/api/v1/videos", files={"file": ("x.mp4", video_file.read_bytes())}).json()["error"]["code"] == "duration_limit"
    unsupported = tmp_path / "mpeg4.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video_file), "-c:v", "mpeg4", str(unsupported)], check=True)
    settings = Settings(data_dir=tmp_path / "codec")
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.post("/api/v1/videos", files={"file": ("x.mp4", unsupported.read_bytes())})
        assert response.status_code == 201
        assert response.json()["video_codec"] == "mpeg4"


def test_active_dedup_retry_guard_and_recovery(client, app, video_file):
    video = upload(client, video_file)
    first = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()
    again = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()
    assert first["task_id"] == again["task_id"]
    assert client.post(f'/api/v1/tasks/{first["task_id"]}/retry').status_code == 409
    assert client.get(f'/api/v1/analyses/{first["task_id"]}').status_code == 409
    with app.state.sessions.begin() as session:
        task = session.get(Task, first["task_id"])
        task.status = "running"
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, app.state.pipeline)})
    worker.start()
    worker.stop()
    state = client.get(f'/api/v1/tasks/{first["task_id"]}').json()
    assert state["status"] == "interrupted"
    assert client.post(f'/api/v1/tasks/{first["task_id"]}/retry').status_code == 202
    assert client.get(f'/api/v1/tasks/{first["task_id"]}').json()["attempts"] == 2


def test_filename_not_used_as_storage_path(client, video_file):
    response = client.post("/api/v1/videos", files={"file": ("../../secret.mp4", video_file.read_bytes())})
    assert response.status_code == 201
    assert response.json()["filename"] == "secret.mp4"


def test_upload_saves_unprocessed_file_without_creating_task(client, app, video_file):
    video = upload(client, video_file)
    with app.state.sessions() as session:
        assert session.scalar(select(Task)) is None
    listed = client.get("/api/v1/videos").json()
    assert listed[0]["id"] == video["id"]
    assert listed[0]["latest_task"] is None
    assert listed[0]["created_at"]


def test_library_includes_all_files_over_one_hundred(client, app):
    with app.state.sessions.begin() as session:
        for index in range(105):
            session.add(Video(id=f"file-{index:03}", fingerprint=f"hash-{index}", filename=f"文件-{index}.mp4",
                              size_bytes=1, duration=2, video_codec="h264", pinned=index == 0,
                              created_at=f"2026-10-11T00:{index // 60:02}:{index % 60:02}+00:00"))
        session.flush()
        session.add(Task(id="done", video_id="file-001", status="succeeded", config={}, created_at="2026-10-12T00:00:00+00:00"))
    files = client.get("/api/v1/videos").json()
    assert len(files) == 105
    assert files[0]["id"] == "file-000"
    assert files[1]["id"] == "file-001"
    assert files[2]["id"] == "file-104"
    assert next(file for file in files if file["id"] == "file-001")["latest_task"]["status"] == "succeeded"
    assert files[0]["latest_task"] is None
