"""Deterministic settings commit and graceful worker shutdown regressions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import json

import httpx
import pytest

from app.analysis.worker import Worker
from app.db import Task
from app.settings import settings_service as settings_module
from app.settings.model_settings import ModelSettingsInput
from app.settings.model_settings import NoteStyleInput
from app.settings.model_profiles import ProfileInput
from app.settings.network_settings import NetworkSettingsInput, effective_proxy
from app.db import DocumentTask, Video
from app.documents.model import DocumentModelClient
from app.videos.model import ModelClient as VideoModelClient
from .fakes import FakeModel, FakeRecognizer
from .conftest import upload


def test_concurrent_settings_compute_from_last_committed_snapshot(app, monkeypatch):
    service = app.state.settings_service
    saving, release, second_attempt = threading.Event(), threading.Event(), threading.Event()
    underlying = threading.Lock()
    class ObservedLock:
        def __enter__(self):
            if saving.is_set():
                second_attempt.set()
            underlying.acquire()
        def __exit__(self, *args):
            underlying.release()
    service._write_lock = ObservedLock()
    monkeypatch.setattr(settings_module, "recognition_candidate", lambda current, value: replace(current, asr_language="en"))
    original_save, original_candidate = settings_module.save_recognition, settings_module.candidate
    def save(settings):
        saving.set()
        assert release.wait(5)
        original_save(settings)
    def candidate(current, value):
        result = original_candidate(current, value)
        second_attempt.set()
        return result
    monkeypatch.setattr(settings_module, "save_recognition", save)
    monkeypatch.setattr(settings_module, "candidate", candidate)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(service.update_recognition, None)
        assert saving.wait(5)
        second = executor.submit(lambda: asyncio.run(service.update_model_settings(ModelSettingsInput(model="new-model"))))
        try:
            assert second_attempt.wait(5)
        finally:
            release.set()
        first.result(5)
        second.result(5)
    assert service.current.asr_language == "en" and service.current.llm_model == "new-model"
    loaded = settings_module.load_application_settings(replace(service.current, asr_language="zh", llm_model="original"))
    assert loaded.asr_language == "en" and loaded.llm_model == "new-model"


def test_worker_stop_notifies_active_task_and_waits_outside_dispatch_lock(client, app, video_file):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    started, cancelled, release, stopped = (threading.Event() for _ in range(4))
    class Handler:
        diagnostics = app.state.diagnostics
        recovery_message = "interrupted"
        def retry_checkpoint_cleanup(self):
            pass
        def run(self, task_id, *, shutdown=None):
            started.set()
            assert release.wait(8)
        def cancel_model(self, task_id):
            assert task_id == identifier
            # The worker must not retain its dispatch lock across this callback.
            assert worker._dispatch_lock.acquire(blocking=False)
            worker._dispatch_lock.release()
            cancelled.set()
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, Handler())})
    worker.start()
    assert started.wait(5)
    def stop():
        try:
            worker.stop()
        finally:
            stopped.set()
    closer = threading.Thread(target=stop)
    closer.start()
    try:
        assert cancelled.wait(1)
        assert not stopped.is_set()
    finally:
        release.set()
        closer.join(5)
        worker.stop()
    assert stopped.is_set() and not worker.thread.is_alive()


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
@pytest.mark.parametrize("user_stop", [False, True])
def test_shutdown_cancels_inflight_generation_keeps_checkpoints_and_resumes(client, app, video_file, monkeypatch, kind, style, user_stop):
    assert client.put("/api/v1/settings/model", json={"token": "fake-shutdown-key"}).status_code == 200
    entered, cleaned = threading.Event(), threading.Event()
    pending, calls = [], []
    blocking = True
    class VideoModel(FakeModel):
        async def generate(self, operation, payload, validate):
            nonlocal blocking
            calls.append(operation)
            if blocking and operation == ("concise" if style == "concise" else "chapters"):
                blocking = False
                self.calls += 1
                future = asyncio.get_running_loop().create_future()
                pending.append((asyncio.get_running_loop(), future))
                entered.set()
                try:
                    await future
                finally:
                    self.cancelled_calls += 1
                    cleaned.set()
            if kind == "document" and operation == "outline":
                self.calls += 1
                self.usage_reports += 1
                value = {"content_type": "课程或教材", "document_summary": "循环条件与终止。",
                    "chapters": [{"title": "循环", "core_question": "如何终止？", "topics": ["条件"], "start_anchor_id": payload["anchors"][0]}]}
                value = validate(value)
            else:
                value = await super().generate(operation, payload, validate)
            return value
    class DocumentModel(DocumentModelClient, VideoModel):
        pass
    if kind == "video":
        video = upload(client, video_file)
        with app.state.sessions.begin() as session:
            session.get(Video, video["id"]).audio_codec = "aac"
        pipeline, table = app.state.pipeline, Task
        pipeline.recognizer_factory, pipeline.model_factory = FakeRecognizer, VideoModel
        monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, output, media_info=None, cancel=None: output.write_bytes(b"audio"))
        base = f'/api/v1/videos/{video["id"]}'
    else:
        response = client.post("/api/v1/documents", files={"file": ("course.txt", "循环由条件控制，更新状态后再次判断。".encode(), "text/plain")})
        assert response.status_code == 201, response.text
        pipeline, table = app.state.document_pipeline, DocumentTask
        pipeline.model_factory = DocumentModel
        base = f'/api/v1/documents/{response.json()["id"]}'
    response = client.post(base + "/analyses", json={"note_style": style})
    assert response.status_code == 202, response.text
    identifier = response.json()["task_id"]
    worker = Worker(app.state.sessions, app.state.diagnostics, {kind: (table, pipeline)})
    worker.start()
    with ThreadPoolExecutor(1) as executor:
        try:
            assert entered.wait(5)
            if user_stop:
                control = "tasks" if kind == "video" else "document-tasks"
                assert client.post(f"/api/v1/{control}/{identifier}/stop").status_code in {200, 202}
            executor.submit(worker.stop).result(5)
        finally:
            for loop, future in pending:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(future.cancel)
            worker.stop()
    assert cleaned.is_set() and not worker.thread.is_alive()
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.status == ("paused" if user_stop else "interrupted") and task.attempts == 1
        assert task.error is None if user_stop else task.error["code"] == "interrupted"
        assert "execution_stats" not in task.config
        assert task.timings["total"] > 0
        assert ("concise" if style == "concise" else "summarizing") in task.timings
    with app.state.sessions.begin() as session:
        session.get(table, identifier).status = "queued"
    before = len(calls)
    pipeline.run(identifier)
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.status == "succeeded", task.error
        assert "execution_stats" not in task.config
        final = dict(task.result)
    assert len(calls) > before
    if style == "detailed":
        assert calls.count("outline") == 1
    pipeline.run(identifier)
    with app.state.sessions() as session:
        assert session.get(table, identifier).result == final


def test_shutdown_after_dequeue_keeps_unclaimed_task_queued(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    worker = app.state.worker
    dequeued, release = threading.Event(), threading.Event()
    original = worker.pending.get
    def get(*args, **kwargs):
        item = original(*args, **kwargs)
        dequeued.set()
        assert release.wait(5)
        return item
    monkeypatch.setattr(worker.pending, "get", get)
    monkeypatch.setattr(app.state.pipeline, "run", lambda *args, **kwargs: pytest.fail("关闭后不得开始任务"))
    worker.start()
    assert dequeued.wait(5)
    with ThreadPoolExecutor(1) as executor:
        stopping = executor.submit(worker.stop)
        try:
            assert worker.stopping.wait(5)
        finally:
            release.set()
        stopping.result(5)
    worker.stop()
    with app.state.sessions() as session:
        assert session.get(Task, identifier).status == "queued"


def test_stopping_unstarted_worker_is_safe_and_repeatable(app):
    app.state.worker.stop()
    app.state.worker.stop()
    assert not app.state.worker.thread.is_alive()


@pytest.mark.parametrize("first_kind", ["network", "style"])
def test_concurrent_profile_edit_preserves_other_settings_and_saved_files(app, monkeypatch, first_kind):
    service = app.state.settings_service
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    underlying = threading.Lock()
    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                attempted.set()
            underlying.acquire()
        def __exit__(self, *args):
            underlying.release()
    service._write_lock = ObservedLock()
    save_name = "save_network_settings" if first_kind == "network" else "save_settings"
    original_save = getattr(settings_module, save_name)
    original_edit = settings_module.model_profiles.edit
    def save(settings):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        original_save(settings)
    def edit(*args):
        result = original_edit(*args)
        attempted.set()
        return result
    monkeypatch.setattr(settings_module, save_name, save)
    monkeypatch.setattr(settings_module.model_profiles, "edit", edit)
    def first():
        if first_kind == "network":
            return asyncio.run(service.update_network_settings(NetworkSettingsInput(enabled=True, url="http://127.0.0.1:7890")))
        return asyncio.run(service.update_note_style(NoteStyleInput(summary_prompt="保留条件", note_style="concise")))
    with ThreadPoolExecutor(2) as executor:
        one = executor.submit(first)
        assert entered.wait(5)
        two = executor.submit(service.update_model_profile, ProfileInput(name="test", model="profile-model"), "default")
        try:
            assert attempted.wait(5)
        finally:
            release.set()
        one.result(5)
        two.result(5)
    loaded = settings_module.load_application_settings(replace(service.current, llm_profiles=(), llm_profile_id=""))
    for settings in (service.current, loaded):
        assert settings.llm_model == "profile-model"
        if first_kind == "network":
            assert settings.video_proxy_enabled and settings.video_proxy_url == "http://127.0.0.1:7890"
        else:
            assert settings.note_style == "concise" and settings.summary_prompt == "保留条件"


@pytest.mark.parametrize("operation", ["write", "replace"])
@pytest.mark.parametrize("area", ["model", "network"])
def test_settings_failure_does_not_publish_and_releases_commit_lock(app, monkeypatch, operation, area):
    from pathlib import Path
    import os
    service = app.state.settings_service
    previous = service.current
    proxy = effective_proxy(app.state.video_downloads.get_settings())
    name = "model-settings" if area == "model" else "network-settings"
    def fail(*args, **kwargs):
        raise OSError("simulated settings failure")
    target = os if operation == "write" else Path
    attribute = "open" if operation == "write" else "replace"
    with monkeypatch.context() as patch:
        original = getattr(target, attribute)
        def guarded(source, *args, **kwargs):
            filename = Path(source).name
            if filename == name + ".tmp" or filename.startswith("." + name + ".json.") and filename.endswith(".tmp"):
                return fail()
            return original(source, *args, **kwargs)
        patch.setattr(target, attribute, guarded)
        with pytest.raises(OSError, match="private_write_failed" if area == "model" else "simulated settings failure"):
            if area == "model":
                asyncio.run(service.update_model_settings(ModelSettingsInput(model="changed")))
            else:
                asyncio.run(service.update_network_settings(NetworkSettingsInput(enabled=True, url="http://127.0.0.1:7890")))
    assert service.current is previous and app.state.pipeline.get_settings() is previous
    assert effective_proxy(app.state.video_downloads.get_settings()) == proxy
    assert service._write_lock.acquire(blocking=False)
    service._write_lock.release()
    asyncio.run(service.update_model_settings(ModelSettingsInput(model="retry-ok")))
    assert service.current.llm_model == "retry-ok"


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
@pytest.mark.parametrize("boundary", ["stream", "interval", "retry"])
def test_shutdown_wakes_real_client_at_all_request_wait_boundaries(client, app, video_file, monkeypatch, kind, style, boundary):
    from app.analysis.llm import RequestPacer
    entered, cleaned = threading.Event(), threading.Event()
    waits, sent = [], []
    assert client.put("/api/v1/settings/model", json={"token": "fake-shutdown", "stream": True}).status_code == 200
    async def block():
        future = asyncio.get_running_loop().create_future()
        waits.append((asyncio.get_running_loop(), future))
        entered.set()
        try:
            await future
        finally:
            cleaned.set()
    if boundary == "interval":
        async def paced(self):
            await block()
        monkeypatch.setattr(RequestPacer, "wait", paced)
    elif boundary == "retry":
        original_sleep = asyncio.sleep
        async def sleep(delay):
            if delay >= 3:
                await block()
            else:
                await original_sleep(delay)
        monkeypatch.setattr(asyncio, "sleep", sleep)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
            await block()
        async def aclose(self):
            cleaned.set()
    async def transport(self, request):
        sent.append(json.loads(request.content))
        if boundary == "retry":
            return httpx.Response(503, content=b'{"error":{"message":"unavailable"}}', request=request)
        return httpx.Response(200, stream=Stream(), request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    if kind == "video":
        video = upload(client, video_file)
        pipeline, table = app.state.pipeline, Task
        pipeline.recognizer_factory, pipeline.model_factory = FakeRecognizer, VideoModelClient
        monkeypatch.setattr("app.videos.video_reader.UniformFrameReader.save", lambda self, output, seconds, cancel=None: output.write_bytes(b"frame"))
        base = f'/api/v1/videos/{video["id"]}'
    else:
        doc = client.post("/api/v1/documents", files={"file": ("course.txt", "循环原理与条件。".encode(), "text/plain")}).json()
        pipeline, table = app.state.document_pipeline, DocumentTask
        pipeline.model_factory = DocumentModelClient
        base = f'/api/v1/documents/{doc["id"]}'
    response = client.post(base + "/analyses", json={"note_style": style})
    assert response.status_code == 202, response.text
    identifier = response.json()["task_id"]
    worker = Worker(app.state.sessions, app.state.diagnostics, {kind: (table, pipeline)})
    worker.start()
    with ThreadPoolExecutor(1) as executor:
        try:
            assert entered.wait(5)
            executor.submit(worker.stop).result(5)
        finally:
            for loop, future in waits:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(future.cancel)
            worker.stop()
    assert cleaned.is_set()
    assert len(sent) == (0 if boundary == "interval" else 1)
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.status == "interrupted", task.error
        assert "execution_stats" not in task.config
        requests = task.config.get("request_count", 0) if kind == "video" else task.request_count
        assert requests == len(sent)


@pytest.mark.parametrize("operation", ["asr", "ocr"])
@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_waits_for_native_safe_point_without_fallback(client, app, video_file, monkeypatch, operation, fails):
    from types import SimpleNamespace
    entered, release = threading.Event(), threading.Event()
    clock = [0.0]
    monkeypatch.setattr("app.videos.pipeline.time", SimpleNamespace(monotonic=lambda: clock[0]))
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    class Recognizer(FakeRecognizer):
        def wait(self):
            entered.set()
            assert release.wait(5)
            clock[0] += 1.0
            if fails:
                raise OSError("native operation stopped")
        def transcribe(self, path):
            if operation == "asr":
                self.wait()
            return super().transcribe(path)
        def ocr(self, path):
            if operation == "ocr":
                self.wait()
            return super().ocr(path)
    pipeline = app.state.pipeline
    pipeline.recognizer_factory, pipeline.model_factory = Recognizer, FakeModel
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, output, media_info=None, cancel=None: output.write_bytes(b"audio"))
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, pipeline)})
    worker.start()
    with ThreadPoolExecutor(1) as executor:
        assert entered.wait(5)
        stopping = executor.submit(worker.stop)
        try:
            assert worker.stopping.wait(5)
            assert not stopping.done()
        finally:
            release.set()
        stopping.result(5)
    with app.state.sessions() as session:
        task = session.get(Task, identifier)
        assert task.status == "interrupted" and task.warnings == []
        assert task.timings["transcribing" if operation == "asr" else "ocr"] > 0
    if operation == "asr":
        assert pipeline.recognizer.ocr_calls == 0


def test_subtitle_shutdown_does_not_fall_back_to_asr(client, app, video_file, monkeypatch):
    from app.errors import AppError
    entered = threading.Event()
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        stored = session.get(Video, video["id"])
        stored.media_info = {**stored.media_info, "source": {"platform": "youtube", "url": "https://youtube.com/watch?v=fake"}}
    def subtitles(source, language, duration, folder, proxy, cancel, **kwargs):
        entered.set()
        assert cancel.wait(5)
        raise AppError("media_cancelled", "cancelled")
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", subtitles)
    app.state.pipeline.recognizer_factory = FakeRecognizer
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    worker = app.state.worker
    worker.start()
    assert entered.wait(5)
    worker.stop()
    with app.state.sessions() as session:
        task = session.get(Task, identifier)
        assert task.status == "interrupted" and task.warnings == []
    assert app.state.pipeline.recognizer.asr_calls == app.state.pipeline.recognizer.ocr_calls == 0


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("when", ["client_creation", "after_success_commit"])
def test_shutdown_registration_race_and_committed_success(client, app, video_file, monkeypatch, kind, when):
    shutdown = threading.Event()
    instances = []
    class DocumentModel(DocumentModelClient, FakeModel):
        pass
    client.put("/api/v1/settings/model", json={"token": "fake-race"})
    if kind == "video":
        video = upload(client, video_file)
        pipeline, table, factory = app.state.pipeline, Task, FakeModel
        pipeline.recognizer_factory = FakeRecognizer
        base = f'/api/v1/videos/{video["id"]}'
    else:
        doc = client.post("/api/v1/documents", files={"file": ("course.txt", "循环原理。".encode(), "text/plain")}).json()
        pipeline, table, factory = app.state.document_pipeline, DocumentTask, DocumentModel
        base = f'/api/v1/documents/{doc["id"]}'
    def model(settings):
        if when == "client_creation":
            shutdown.set()
        value = factory(settings)
        instances.append(value)
        return value
    pipeline.model_factory = model
    if when == "after_success_commit":
        if kind == "video":
            original = pipeline.update
            def update(identifier, **values):
                original(identifier, **values)
                if values.get("status") == "succeeded":
                    shutdown.set()
            monkeypatch.setattr(pipeline, "update", update)
        else:
            original = pipeline.succeed
            def succeed(*args):
                original(*args)
                shutdown.set()
            monkeypatch.setattr(pipeline, "succeed", succeed)
    identifier = client.post(base + "/analyses", json={"note_style": "concise"}).json()["task_id"]
    pipeline.run(identifier, shutdown=shutdown)
    with app.state.sessions() as session:
        task = session.get(table, identifier)
        assert task.status == ("interrupted" if when == "client_creation" else "succeeded"), task.error
        assert "execution_stats" not in task.config
        assert sum(model.calls for model in instances) == (0 if when == "client_creation" else 1)
        assert task.result is None if when == "client_creation" else task.result["complete"]


def test_lifespan_closes_resources_after_native_exit_and_timings(app, video_file, monkeypatch, tmp_path):
    from app.main import create_app
    from app.videos.pipeline import Pipeline
    from fastapi.testclient import TestClient
    entered, release = threading.Event(), threading.Event()
    events, identifiers = [], []
    class Recognizer(FakeRecognizer):
        def ocr(self, path):
            entered.set()
            assert release.wait(5)
            return super().ocr(path)
    def pipeline_factory(get_settings, sessions, diagnostics):
        return Pipeline(get_settings, sessions, diagnostics, Recognizer, FakeModel)
    settings = replace(app.state.pipeline.get_settings(), data_dir=tmp_path / "lifecycle", log_dir=tmp_path / "lifecycle-log")
    composed = create_app(settings, pipeline_factory=pipeline_factory)
    for owner, method in ((composed.state.playback, "close"), (composed.state.video_downloads, "close"), (composed.state.diagnostics, "close")):
        original = getattr(owner, method)
        def close(original=original, owner=owner):
            assert not composed.state.worker.thread.is_alive()
            with composed.state.sessions() as session:
                task = session.get(Task, identifiers[0])
                assert task.status == "interrupted" and task.timings["total"] > 0
                assert "execution_stats" not in task.config
            events.append(owner)
            original()
        monkeypatch.setattr(owner, method, close)
    def serve():
        with TestClient(composed) as client:
            video = upload(client, video_file)
            identifiers.append(client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"])
            assert entered.wait(5)
    with ThreadPoolExecutor(1) as executor:
        serving = executor.submit(serve)
        try:
            assert entered.wait(5)
            assert composed.state.worker.stopping.wait(5)
            assert not events and not serving.done()
        finally:
            release.set()
        serving.result(5)
    assert events == [composed.state.playback, composed.state.video_downloads, composed.state.diagnostics]


def test_model_cleanup_failure_keeps_original_exit_reason(app):
    from app.analysis.llm import ModelClient
    from app.errors import TaskInterrupted
    model = ModelClient(app.state.pipeline.get_settings())
    model.diagnostics = app.state.diagnostics
    class BrokenClient:
        async def aclose(self):
            raise OSError("close failure")
    model.client = BrokenClient()
    asyncio.run(model.__aexit__(TaskInterrupted, TaskInterrupted(), None))
    with pytest.raises(OSError, match="close failure"):
        asyncio.run(model.__aexit__(None, None, None))
