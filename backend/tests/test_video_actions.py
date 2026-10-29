import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
from app.db import Evidence, Task, Video, database
from app.main import create_app
from .conftest import upload


def test_legacy_database_adds_pinned_without_losing_video(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE videos (id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, filename TEXT, size_bytes INTEGER, duration REAL, video_codec TEXT, audio_codec TEXT, created_at TEXT)")
        connection.execute("INSERT INTO videos VALUES ('old', 'hash', '旧视频.mp4', 12, 2, 'h264', NULL, '2026-10-09')")
    engine, sessions = database(path)
    with sessions() as session:
        video = session.get(Video, "old")
        assert video.filename == "旧视频.mp4" and video.pinned is False
    engine.dispose()


def test_rename_pin_persist_order_and_validation(client, app, video_file):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.add(Video(id="newer", fingerprint="other", filename="新视频.mp4", size_bytes=1, duration=2,
                          video_codec="h264", created_at="2099-01-01"))
    endpoint = f'/api/v1/videos/{video["id"]}'
    assert client.get("/api/v1/videos").json()[0]["id"] == "newer"
    assert client.patch(endpoint, json={"filename": "  网络基础  ", "pinned": True}).status_code == 200
    assert client.get("/api/v1/videos").json()[0]["filename"] == "网络基础"
    with TestClient(create_app(app.state.pipeline.get_settings(), start_worker=False)) as restarted:
        first = restarted.get("/api/v1/videos").json()[0]
        assert first["id"] == video["id"] and first["pinned"] is True
    assert client.get(endpoint + "/content", headers={"Range": "bytes=0-31"}).status_code == 206
    for value in ({}, {"filename": " "}, {"filename": "../bad"}, {"filename": "x\\y"}, {"filename": "x\ny"}, {"filename": "x" * 201}, {"token": "fake"}):
        assert client.patch(endpoint, json=value).status_code == 422
    assert client.patch(endpoint, json={"pinned": False}).status_code == 200
    assert client.get("/api/v1/videos").json()[0]["id"] == "newer"


def test_list_orders_by_latest_generation_with_pins_and_upload_fallback(client, app):
    with app.state.sessions.begin() as session:
        for identifier, created, pinned in (("older", "2020-01-01", False),
                                            ("newer", "2021-01-01", False),
                                            ("pinned", "2019-01-01", True)):
            session.add(Video(id=identifier, fingerprint=identifier, filename=f"{identifier}.mp4",
                              size_bytes=1, duration=2, video_codec="h264", created_at=created, pinned=pinned))

    def order():
        return [video["id"] for video in client.get("/api/v1/videos").json()]

    assert order() == ["pinned", "newer", "older"]
    first = client.post("/api/v1/videos/older/analyses").json()
    assert order() == ["pinned", "older", "newer"]
    second = client.post("/api/v1/videos/newer/analyses").json()
    assert order() == ["pinned", "newer", "older"]
    reused = client.post("/api/v1/videos/older/analyses").json()
    assert reused["reused"] is True and reused["task_id"] == first["task_id"]
    assert order() == ["pinned", "newer", "older"]
    with app.state.sessions.begin() as session:
        session.get(Task, first["task_id"]).status = "succeeded"
        session.get(Task, second["task_id"]).status = "succeeded"
    regenerated = client.post("/api/v1/videos/older/analyses").json()
    assert regenerated["task_id"] != first["task_id"] and regenerated["reused"] is False
    assert order() == ["pinned", "older", "newer"]
    with TestClient(create_app(app.state.pipeline.get_settings(), start_worker=False)) as restarted:
        assert [video["id"] for video in restarted.get("/api/v1/videos").json()] == order()


def test_progress_edits_and_rejected_generation_do_not_change_file_order(client, app):
    with app.state.sessions.begin() as session:
        for identifier in ("first", "second"):
            session.add(Video(id=identifier, fingerprint=identifier, filename=f"{identifier}.mp4",
                              size_bytes=1, duration=2, video_codec="h264", created_at="2020-01-01"))
        session.flush()
        session.add_all([
            Task(id="old-task", video_id="first", status="failed", config={},
                 created_at="2021-01-01", updated_at="2099-01-01"),
            Task(id="new-task", video_id="second", status="running", config={},
                 created_at="2022-01-01", updated_at="2022-01-01"),
        ])
    assert client.patch("/api/v1/videos/first", json={"filename": "改名.mp4"}).status_code == 200
    assert client.post("/api/v1/videos/first/analyses", json={"language": "invalid"}).status_code == 422
    assert [video["id"] for video in client.get("/api/v1/videos").json()] == ["second", "first"]


def test_delete_cleans_only_selected_video_and_blocks_active(client, app, video_file):
    video = upload(client, video_file)
    endpoint = f'/api/v1/videos/{video["id"]}'
    task_id = client.post(endpoint + "/analyses").json()["task_id"]
    folder = app.state.pipeline.get_settings().data_dir / "videos" / video["id"]
    assert client.delete(endpoint).status_code == 409
    assert folder.exists()
    assert client.post(f'/api/v1/tasks/{task_id}/stop').json()["status"] == "paused"
    with app.state.sessions.begin() as session:
        session.add(Evidence(id="source", video_id=video["id"], kind="speech", start_seconds=0, end_seconds=1, text="原文"))
    unrelated = folder.parent / "keep"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    assert client.delete(endpoint).json() == {"deleted": True, "warning": None}
    assert not folder.exists() and (unrelated / "keep.txt").is_file()
    assert client.get(endpoint + "/content").status_code == 404
    assert client.get(f'/api/v1/tasks/{task_id}').status_code == 404
    assert client.get("/api/v1/evidence/source").status_code == 404
    assert client.get("/api/v1/videos").json() == []


def test_delete_filesystem_failure_preserves_record(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    def fail(*args):
        raise PermissionError("test: file is locked")
    monkeypatch.setattr(Path, "rename", fail)
    assert client.delete(f'/api/v1/videos/{video["id"]}').status_code == 500
    assert client.get("/api/v1/videos").json()[0]["id"] == video["id"]
    assert client.get(f'/api/v1/videos/{video["id"]}/content').status_code == 200
