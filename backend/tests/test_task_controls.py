import threading
from dataclasses import replace
import pytest
from app.db import Task, Video
from app.videos.pipeline import Pipeline
from app.analysis.worker import Worker
from .conftest import upload
from .fakes import FakeRecognizer, FakeModel


def test_queued_stop_resume_restart_and_guards(client, app, video_file):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    assert client.post(f'/api/v1/tasks/{task_id}/stop').json()["status"] == "paused"
    app.state.pipeline.run(task_id)  # 已从队列取出但用户先停止，不能覆盖成运行中。
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "paused"
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, app.state.pipeline)})
    worker.start()
    worker.stop()
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "paused"
    assert client.post(f'/api/v1/tasks/{task_id}/stop').json()["status"] == "paused"
    assert client.post(f'/api/v1/tasks/{task_id}/resume').status_code == 202
    assert client.post(f'/api/v1/tasks/{task_id}/resume').status_code == 409
    assert client.delete(f'/api/v1/videos/{video["id"]}').status_code == 409
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "stopping"
    worker = Worker(app.state.sessions, app.state.diagnostics, {"video": (Task, app.state.pipeline)})
    worker.start()
    worker.stop()
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "paused"


@pytest.mark.parametrize('kind', ['video', 'document'])
@pytest.mark.parametrize('action', ['resume', 'retry'])
def test_old_task_rejoins_mixed_queue_and_remains_current_after_restart(client, app, video_file, monkeypatch, kind, action):
    from app.db import DocumentTask
    video = upload(client, video_file)
    doc = client.post('/api/v1/documents', files={'file':('queue.txt', b'queue material')}).json()
    client.put('/api/v1/settings/model', json={'token':'isolated-queue-key'})
    paths = {'video': f'/api/v1/videos/{video["id"]}', 'document': f'/api/v1/documents/{doc["id"]}'}
    controls = {'video': '/api/v1/tasks/', 'document': '/api/v1/document-tasks/'}
    tables = {'video': Task, 'document': DocumentTask}
    def create(which):
        response = client.post(paths[which] + '/analyses')
        assert response.status_code == 202, response.text
        return response.json()['task_id']
    first = create(kind)
    assert client.post(controls[kind] + first + '/stop').status_code == 202
    second = create(kind)
    assert client.post(controls[kind] + second + '/stop').status_code == 202
    other_kind = 'document' if kind == 'video' else 'video'
    other = create(other_kind)
    if action == 'retry':
        with app.state.sessions.begin() as session:
            session.get(tables[kind], first).status = 'failed'
    assert client.post(controls[kind] + first + '/' + action).status_code == 202
    before = [(k, identifier) for k, identifier in app.state.worker.pending.queue
              if client.get(controls[k] + identifier).json()['status'] == 'queued']
    assert before == [(other_kind, other), (kind, first)]
    for _ in range(2):
        listed = client.get('/api/v1/videos' if kind == 'video' else '/api/v1/documents').json()
        assert listed[0]['latest_task'] == client.get(controls[kind] + first).json()
    worker = Worker(app.state.sessions, app.state.diagnostics,
                    {'video': (Task, app.state.pipeline), 'document': (DocumentTask, app.state.document_pipeline)})
    monkeypatch.setattr(worker.thread, 'start', lambda: None)
    worker.start()
    assert list(worker.pending.queue) == before
    # An inactive newer update must not hide the active task either.
    with app.state.sessions.begin() as session:
        session.get(tables[kind], second).updated_at = '9999'
    listed = client.get('/api/v1/videos' if kind == 'video' else '/api/v1/documents').json()
    assert listed[0]['latest_task']['id'] == first
    assert client.post(controls[kind] + first + '/stop').status_code == 202
    listed = client.get('/api/v1/videos' if kind == 'video' else '/api/v1/documents').json()
    assert listed[0]['latest_task']['id'] == second
    with app.state.sessions.begin() as session:
        for identifier in (first, second):
            session.get(tables[kind], identifier).updated_at = 'same-time'
    listed = client.get('/api/v1/videos' if kind == 'video' else '/api/v1/documents').json()
    assert listed[0]['latest_task']['id'] == max(first, second)


@pytest.mark.parametrize("pause_at", ["ocr", "outline", "chapters", "overview"])
def test_resume_reuses_checkpoints_and_keeps_snapshot(client, app, video_file, monkeypatch, pause_at):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).duration = 360
    monkeypatch.setattr("app.videos.pipeline.extract_frame", lambda media, image, seconds, media_info=None: image.write_bytes(b"test frame"))
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    paused = False
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.config = {k: v for k, v in task.config.items() if k != "frame_count"}
    counts = {"ocr": 0, "outline": 0, "chapters": 0, "overview": 0}
    models = []

    def stop(stage):
        nonlocal paused
        if stage == pause_at and not paused:
            paused = True
            assert client.post(f'/api/v1/tasks/{task_id}/stop').json()["status"] == "stopping"
            assert client.post(f'/api/v1/tasks/{task_id}/resume').status_code == 409
            assert client.delete(f'/api/v1/videos/{video["id"]}').status_code == 409

    class Recognizer(FakeRecognizer):
        def ocr(self, image):
            counts["ocr"] += 1
            value = super().ocr(image)
            stop("ocr")
            return value

    class Model(FakeModel):
        def __init__(self, settings):
            super().__init__(settings)
            models.append((settings.llm_model, settings.summary_prompt))

        async def generate(self, operation, payload, validate):
            counts[operation] += 1
            value = await super().generate(operation, payload, validate)
            stop(operation)
            return value

    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, Recognizer, Model)
    app.state.worker.register("video", Task, pipeline)
    original = (pipeline.get_settings().llm_model, pipeline.get_settings().summary_prompt)
    pipeline.run(task_id)
    state = client.get(f'/api/v1/tasks/{task_id}').json()
    assert state["status"] == "paused" and state["error"] is None
    assert state["progress"]["percent"] > 0
    assert client.get(f'/api/v1/videos/{video["id"]}/transcript', params={"task_id": task_id}).status_code == 200
    if pause_at == "ocr":
        assert counts == {"ocr": 1, "outline": 0, "chapters": 0, "overview": 0}
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_model="changed/default", summary_prompt="Changed prompt"): snapshot
    assert client.post(f'/api/v1/tasks/{task_id}/resume').status_code == 202
    pipeline.run(task_id)
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "succeeded"
    assert counts == {"ocr": 24, "outline": 1, "chapters": 1, "overview": 1}
    assert all(model == original for model in models)
    assert client.post(f'/api/v1/tasks/{task_id}/stop').status_code == 409
    assert client.post(f'/api/v1/tasks/{task_id}/resume').status_code == 409


def test_running_stop_waits_for_current_operation(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    started, release = threading.Event(), threading.Event()
    class SlowRecognizer(FakeRecognizer):
        def ocr(self, image):
            started.set()
            assert release.wait(5)
            return super().ocr(image)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, SlowRecognizer, FakeModel)
    thread = threading.Thread(target=pipeline.run, args=(task_id,))
    thread.start()
    try:
        assert started.wait(5)
        assert client.post(f'/api/v1/tasks/{task_id}/stop').json()["status"] == "stopping"
        assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "stopping"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert client.get(f'/api/v1/tasks/{task_id}').json()["status"] == "paused"
    assert client.get(f'/api/v1/videos/{video["id"]}/evidence').json()
