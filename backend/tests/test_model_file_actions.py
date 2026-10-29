from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Task
from app.recognition.local_models import MODELS, model_path
from .conftest import upload
from .support import fake_files


@pytest.mark.parametrize("identifier", ["paraformer", "whisper-small", "whisper-base"])
def test_reveal_and_delete_only_selected_model(client, app, monkeypatch, identifier):
    settings = app.state.pipeline.get_settings()
    fake_files(settings, identifier)
    directory = model_path(settings, identifier).resolve()
    (directory / "unfinished.part").write_bytes(b"partial")
    other = settings.model_dir / "keep-model" / "weights.bin"
    other.parent.mkdir()
    other.write_bytes(b"keep")
    app.state.downloads.update(identifier, status="succeeded", message="ready")
    opened = []
    monkeypatch.setattr("app.recognition.local_models.os.startfile", opened.append, raising=False)
    route = f"/api/v1/settings/models/{identifier}"
    assert client.post(route + "/reveal", json={"path": "C:/Windows"}).json() == {"opened": True}
    assert opened == [str(directory)]
    assert client.delete(route).json() == {"deleted": True}
    assert not directory.exists()
    assert other.read_bytes() == b"keep"
    assert identifier not in app.state.downloads.states
    assert not (app.state.downloads.directory / f"{identifier}.json").exists()
    assert app.state.pipeline.get_settings() is settings
    assert app.state.pipeline.recognizer is None
    catalog = client.get("/api/v1/settings/recognition").json()
    model = next(item for item in catalog["models"] if item["id"] == identifier)
    assert not model["directory_exists"] and not model["ready"] and model["download"] is None
    assert client.delete(route).json() == {"deleted": True}
    assert client.post(route + "/reveal").status_code == 404


def test_partial_model_directory_can_be_removed(client, app):
    path = model_path(app.state.pipeline.get_settings(), "whisper-base")
    path.mkdir(parents=True)
    (path / "model.bin.part").write_bytes(b"partial")
    models = client.get("/api/v1/settings/recognition").json()["models"]
    partial = next(item for item in models if item["id"] == "whisper-base")
    assert partial["directory_exists"] and not partial["ready"]
    assert client.delete("/api/v1/settings/models/whisper-base").status_code == 200
    assert not path.exists()


@pytest.mark.parametrize("status", ["queued", "running", "stopping"])
def test_active_video_task_blocks_model_delete(client, app, video_file, status):
    video = upload(client, video_file)
    fake_files(app.state.pipeline.get_settings(), "whisper-base")
    path = model_path(app.state.pipeline.get_settings(), "whisper-base") / "model.bin"
    before = path.read_bytes()
    with app.state.sessions.begin() as session:
        session.add(Task(id="active-task", video_id=video["id"], config={}, status=status))
    response = client.delete("/api/v1/settings/models/whisper-base")
    assert response.status_code == 409 and response.json()["error"]["code"] == "model_in_use"
    assert path.read_bytes() == before


def test_downloading_blocks_delete_and_disk_failure_releases_locks(client, app, monkeypatch):
    fake_files(app.state.pipeline.get_settings(), "whisper-base")
    directory = model_path(app.state.pipeline.get_settings(), "whisper-base")
    app.state.downloads.active = "paraformer"
    response = client.delete("/api/v1/settings/models/whisper-base")
    assert response.status_code == 409 and response.json()["error"]["code"] == "download_busy"
    assert directory.exists()
    app.state.downloads.active = None
    with monkeypatch.context() as patch:
        def fail(path):
            raise PermissionError("simulated file in use")
        patch.setattr("app.recognition.local_models.shutil.rmtree", fail)
        response = client.delete("/api/v1/settings/models/whisper-base")
        assert response.json()["error"]["code"] == "model_delete_failed"
        assert directory.exists()
    assert client.delete("/api/v1/settings/models/whisper-base").status_code == 200


def test_delete_holds_download_and_task_operation_locks(client, app, monkeypatch):
    import shutil
    fake_files(app.state.pipeline.get_settings(), "whisper-base")
    entered, release = threading.Event(), threading.Event()
    original = shutil.rmtree
    def remove(path):
        entered.set()
        assert release.wait(5)
        original(path)
    monkeypatch.setattr("app.recognition.local_models.shutil.rmtree", remove)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(client.delete, "/api/v1/settings/models/whisper-base")
        assert entered.wait(5)
        try:
            assert not app.state.downloads.lock.acquire(blocking=False)
            assert not app.state.worker.lock.acquire(blocking=False)
        finally:
            release.set()
        assert pending.result(5).status_code == 200


def test_file_actions_reject_unlisted_models(client):
    identifier = "unknown"
    route = f"/api/v1/settings/models/{identifier}"
    assert client.post(route + "/reveal").status_code == 404
    assert client.delete(route).status_code == 404


@pytest.mark.parametrize("identifier", ["rapidocr"])
def test_ocr_folder_can_be_opened_without_enabling_delete_or_download_controls(client, app, monkeypatch, identifier):
    settings = app.state.pipeline.get_settings()
    route = f"/api/v1/settings/models/{identifier}"
    missing = client.post(route + "/reveal")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "file_missing"
    fake_files(settings, identifier)
    directory = model_path(settings, identifier).resolve()
    before = {name: (directory / name).read_bytes() for name in MODELS[identifier]["files"]}
    opened = []
    monkeypatch.setattr("app.recognition.local_models.os.startfile", opened.append, raising=False)
    assert client.post(route + "/reveal", json={"path": "C:/Windows"}).json() == {"opened": True}
    assert opened == [str(directory)]
    assert client.delete(route).json()["error"]["code"] == "unknown_model"
    for action in ("pause", "resume", "cancel"):
        response = client.post(route + "/download/" + action)
        assert response.status_code == 404 and response.json()["error"]["code"] == "unknown_model"
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert app.state.pipeline.get_settings() is settings


@pytest.mark.parametrize("identifier", ["whisper-base", "rapidocr"])
@pytest.mark.parametrize("mode", ["outside", "root", "junction", "symlink"])
def test_file_actions_reject_unsafe_directories(client, app, monkeypatch, mode, identifier):
    fake_files(app.state.pipeline.get_settings(), identifier)
    directory = model_path(app.state.pipeline.get_settings(), identifier)
    if mode in {"outside", "root"}:
        monkeypatch.setitem(MODELS, identifier, {**MODELS[identifier], "directory": "../keep-outside" if mode == "outside" else "."})
        directory = model_path(app.state.pipeline.get_settings(), identifier)
        directory.mkdir(exist_ok=True)
    else:
        method = "is_junction" if mode == "junction" else "is_symlink"
        original = getattr(Path, method)
        monkeypatch.setattr(Path, method, lambda path: path == directory or original(path))
    sentinel = directory / "keep.txt"
    sentinel.write_bytes(b"keep")
    route = f"/api/v1/settings/models/{identifier}"
    assert client.post(route + "/reveal").json()["error"]["code"] == "invalid_model_path"
    assert client.delete(route).json()["error"]["code"] == ("invalid_model_path" if identifier == "whisper-base" else "unknown_model")
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("identifier", ["whisper-base", "rapidocr"])
def test_file_actions_are_local_only_and_reveal_reports_unavailable_platform(client, app, monkeypatch, identifier):
    fake_files(app.state.pipeline.get_settings(), identifier)
    route = f"/api/v1/settings/models/{identifier}"
    assert client.post(route + "/reveal", headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert client.delete(route, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    monkeypatch.delattr("app.recognition.local_models.os.startfile", raising=False)
    response = client.post(route + "/reveal")
    assert response.json()["error"]["code"] == "model_folder_unavailable"
    assert model_path(app.state.pipeline.get_settings(), identifier).exists()


@pytest.mark.parametrize("identifier", ["rapidocr"])
def test_ocr_folder_open_failure_preserves_files_and_can_retry(client, app, monkeypatch, identifier):
    fake_files(app.state.pipeline.get_settings(), identifier)
    def fail(path):
        raise OSError("simulated folder open failure")
    monkeypatch.setattr("app.recognition.local_models.os.startfile", fail, raising=False)
    route = f"/api/v1/settings/models/{identifier}/reveal"
    response = client.post(route)
    assert response.json()["error"]["code"] == "model_folder_unavailable"
    assert response.json()["error"]["error_id"]
    opened = []
    monkeypatch.setattr("app.recognition.local_models.os.startfile", opened.append)
    assert client.post(route).json() == {"opened": True}
    assert opened == [str(model_path(app.state.pipeline.get_settings(), identifier).resolve())]
