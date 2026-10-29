import asyncio
import hashlib
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.recognition.local_models import MODELS, ModelDownloads, model_path


@pytest.fixture
def source(app, monkeypatch):
    files = {"config.json": b"{}", "model.bin": b"a" * 65536 + b"b" * 65536}
    monkeypatch.setitem(MODELS, "whisper-base", {**MODELS["whisper-base"], "files": list(files)})
    state = SimpleNamespace(files=files, block="body", range_mode="range", entered=threading.Event(),
                            cleaned=threading.Event(), requests=[])
    async def wait():
        state.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            state.cleaned.set()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield files["model.bin"][:65536]
            await wait()
        async def aclose(self):
            state.cleaned.set()
    async def handler(request):
        state.requests.append(request)
        if "/api/models/" in request.url.path:
            if state.block == "metadata":
                await wait()
            return httpx.Response(200, json={"sha": "pinned-commit", "siblings": [
                {"rfilename": name, "size": len(data), "lfs": {"sha256": hashlib.sha256(data).hexdigest()}}
                for name, data in files.items()]})
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "model.bin" and state.block == "headers":
            await wait()
        if name == "model.bin" and state.block == "body":
            return httpx.Response(200, stream=Body())
        data = files[name]
        ranged = request.headers.get("range")
        if ranged and state.range_mode == "rejected":
            return httpx.Response(416)
        if ranged and state.range_mode != "ignored":
            offset = int(ranged.removeprefix("bytes=").removesuffix("-"))
            start = offset + 1 if state.range_mode == "invalid" else offset
            return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}, content=data[offset:])
        return httpx.Response(200, content=data)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return state


def wait_finished(manager):
    manager.thread.join(5)
    assert not manager.thread.is_alive()
    assert manager.active is None and manager.transfer is None


@pytest.mark.parametrize("range_mode", ["range", "ignored", "rejected"])
def test_pause_resume_checks_range_and_reuses_completed_file(client, app, source, range_mode):
    manager = app.state.downloads
    route = "/api/v1/settings/models/whisper-base/download"
    assert client.post(route).status_code == 202
    assert source.entered.wait(5)
    assert client.post(route + "/pause").status_code == 202
    wait_finished(manager)
    directory = model_path(manager.settings, "whisper-base")
    assert source.cleaned.is_set()
    assert (directory / "model.bin.part").read_bytes() == source.files["model.bin"][:65536]
    assert not (directory / "model.bin").exists()
    assert manager.states["whisper-base"]["status"] == "paused"
    assert manager.states["whisper-base"]["downloaded_bytes"] == len(source.files["config.json"]) + 65536
    assert manager.states["whisper-base"]["error"] is None
    assert all(request.url.host == "hf-mirror.com" for request in source.requests)
    source.block, source.range_mode = None, range_mode
    assert client.post(route + "/resume").status_code == 202
    wait_finished(manager)
    assert manager.states["whisper-base"]["status"] == "succeeded"
    assert (directory / "model.bin").read_bytes() == source.files["model.bin"]
    assert not (directory / "model.bin.part").exists()
    downloads = [request for request in source.requests if "/resolve/" in request.url.path]
    assert sum(request.url.path.endswith("config.json") for request in downloads) == 1
    assert any(request.headers.get("range") == "bytes=65536-" for request in downloads)
    assert source.requests[-2].url.path.endswith("/revision/pinned-commit") or "/resolve/pinned-commit/" in source.requests[-2].url.path


@pytest.mark.parametrize("phase", ["metadata", "headers", "body"])
@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_interrupts_pending_network_without_fallback(client, app, source, phase, action):
    source.block = phase
    manager = app.state.downloads
    directory = model_path(manager.settings, "whisper-base")
    directory.mkdir(parents=True)
    previous = directory / "model.bin"
    previous.write_bytes(b"previous valid model")
    route = "/api/v1/settings/models/whisper-base/download"
    assert client.post(route).status_code == 202
    assert source.entered.wait(5)
    assert client.post(route + "/" + action).status_code == 202
    wait_finished(manager)
    assert source.cleaned.is_set()
    status = manager.states["whisper-base"]
    assert status["status"] == ("paused" if action == "pause" else "cancelled")
    assert status["error"] is None
    assert previous.read_bytes() == b"previous valid model"
    assert all(request.url.host == "hf-mirror.com" for request in source.requests)
    if action == "cancel":
        assert not (directory / "model.bin.part").exists()
        assert client.post(route + "/cancel").json()["status"] == "cancelled"


def test_paused_download_can_resume_after_manager_restart_and_cancel_without_thread(client, app, source):
    manager = app.state.downloads
    manager.start("whisper-base")
    assert source.entered.wait(5)
    manager.control("whisper-base", "pause")
    wait_finished(manager)
    restarted = ModelDownloads(manager.settings, app.state.diagnostics)
    source.block = None
    restarted.start("whisper-base", resume=True)
    wait_finished(restarted)
    assert restarted.states["whisper-base"]["status"] == "succeeded"
    assert any(request.headers.get("range") == "bytes=65536-" for request in source.requests)
    restarted.close()

    manager.states["whisper-base"]["status"] = "paused"
    partial = model_path(manager.settings, "whisper-base") / "model.bin.part"
    partial.write_bytes(b"unfinished")
    manager.control("whisper-base", "cancel")
    assert not partial.exists()
    assert (partial.parent / "model.bin").read_bytes() == source.files["model.bin"]


@pytest.mark.parametrize("cancel", [False, True])
def test_control_before_network_registration_and_repeated_close(client, app, monkeypatch, cancel):
    manager = app.state.downloads
    entered, release = threading.Event(), threading.Event()
    original = manager.run
    def delayed(identifier):
        entered.set()
        assert release.wait(5)
        original(identifier)
    monkeypatch.setattr(manager, "run", delayed)
    manager.start("whisper-base")
    assert entered.wait(5)
    assert manager.transfer is None
    manager.control("whisper-base", "pause")
    if cancel:
        manager.control("whisper-base", "cancel")
    release.set()
    wait_finished(manager)
    assert manager.states["whisper-base"]["status"] == ("cancelled" if cancel else "paused")
    manager.close()
    manager.close()


def test_close_pauses_active_download_and_invalid_ranges_never_publish(client, app, source):
    manager = app.state.downloads
    manager.start("whisper-base")
    assert source.entered.wait(5)
    manager.close()
    wait_finished(manager)
    assert source.cleaned.is_set() and manager.states["whisper-base"]["status"] == "paused"
    source.block, source.range_mode = None, "invalid"
    restarted = ModelDownloads(manager.settings, app.state.diagnostics)
    restarted.start("whisper-base", resume=True)
    wait_finished(restarted)
    assert restarted.states["whisper-base"]["error"]["code"] == "model_checksum"
    assert not (model_path(manager.settings, "whisper-base") / "model.bin").exists()
    restarted.close()


def test_controls_require_local_origin_asr_and_valid_state(client, app):
    for action in ("pause", "resume", "cancel"):
        route = "/api/v1/settings/models/whisper-base/download/" + action
        assert client.post(route, headers={"Origin": "https://foreign.invalid"}).status_code == 403
        assert client.post(route).status_code == 409
        assert client.post("/api/v1/settings/models/rapidocr/download/" + action).status_code == 404


@pytest.mark.parametrize("before_commit", [True, False])
def test_cancel_racing_success_preserves_verified_files(client, app, source, monkeypatch, before_commit):
    manager = app.state.downloads
    source.block = None
    entered, release = threading.Event(), threading.Event()
    original = manager.update
    def update(identifier, **values):
        original(identifier, **values)
        if before_commit and values.get("message") == "模型文件校验完成。":
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(manager, "update", update)
    manager.start("whisper-base")
    if before_commit:
        assert entered.wait(5)
        try:
            assert client.post("/api/v1/settings/models/whisper-base/download/cancel").status_code == 202
        finally:
            release.set()
    wait_finished(manager)
    assert manager.states["whisper-base"]["status"] == ("cancelled" if before_commit else "succeeded")
    directory = model_path(manager.settings, "whisper-base")
    assert (directory / "model.bin").read_bytes() == source.files["model.bin"]
    if not before_commit:
        assert client.post("/api/v1/settings/models/whisper-base/download/cancel").status_code == 409
        assert manager.states["whisper-base"]["status"] == "succeeded"


def test_restart_finishes_pending_cancel_without_deleting_complete_files(client, app, source):
    manager = app.state.downloads
    manager.start("whisper-base")
    assert source.entered.wait(5)
    manager.control("whisper-base", "pause")
    wait_finished(manager)
    manager.update("whisper-base", status="cancelling")
    restarted = ModelDownloads(manager.settings, app.state.diagnostics)
    assert restarted.states["whisper-base"]["status"] == "cancelled"
    directory = model_path(manager.settings, "whisper-base")
    assert not (directory / "model.bin.part").exists()
    assert (directory / "config.json").read_bytes() == source.files["config.json"]


@pytest.mark.parametrize("changed_manifest", [False, True])
def test_resume_never_publishes_corrupt_or_stale_partial_file(client, app, source, changed_manifest):
    manager = app.state.downloads
    manager.start("whisper-base")
    assert source.entered.wait(5)
    manager.control("whisper-base", "pause")
    wait_finished(manager)
    if changed_manifest:
        source.files["model.bin"] = b"new model content"
    else:
        (model_path(manager.settings, "whisper-base") / "model.bin.part").write_bytes(b"x" * 65536)
    source.block = None
    manager.start("whisper-base", resume=True)
    wait_finished(manager)
    assert manager.states["whisper-base"]["status"] == "succeeded"
    assert (model_path(manager.settings, "whisper-base") / "model.bin").read_bytes() == source.files["model.bin"]
