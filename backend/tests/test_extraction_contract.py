from dataclasses import replace
import pytest
from app.videos.pipeline import Pipeline
from app.db import Video, Task
from sqlalchemy import event
from .conftest import upload
from .fakes import FakeRecognizer, FakeModel


def test_recognizer_reuse_ignores_llm_and_tracks_recognition(client, app, video_file):
    created = []
    def factory(settings):
        instance = FakeRecognizer(settings)
        created.append(instance)
        return instance
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, factory, FakeModel)
    video = upload(client, video_file)
    def run():
        identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
        pipeline.run(identifier)
        assert client.get(f'/api/v1/tasks/{identifier}').json()["status"] == "succeeded"
    run()
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_model="test/new", llm_key="new", summary_prompt="new"): snapshot
    run()
    assert len(created) == 1
    from .support import fake_files
    client.get("/api/v1/settings/recognition")
    fake_files(pipeline.get_settings(), "whisper-base")
    assert client.put("/api/v1/settings/recognition", json={"asr_option": "whisper-base", "ocr_option": "rapidocr"}).status_code == 200
    run()
    assert len(created) == 2 and created[-1].settings.asr_engine == "whisper"


@pytest.mark.parametrize("frames", [4, 12])
@pytest.mark.parametrize("interruption", [None, "stop", "commit"])
def test_ocr_writes_constant_transcripts_and_recovers_cached_frames(client, app, video_file, monkeypatch, frames, interruption):
    import app.videos.pipeline as module
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).duration = frames
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.config = {**{k: v for k, v in task.config.items() if k != "frame_count"}, "frame_interval": 1}
    settings = replace(app.state.pipeline.get_settings(), frame_interval=1)
    recognizer = FakeRecognizer(settings)
    pipeline = Pipeline(lambda snapshot=settings: snapshot, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    monkeypatch.setattr(module, "extract_frame", lambda source, target, *args: target.write_bytes(b"frame"))
    writes = []
    save = module.save_transcript
    def recording(*args, **kwargs):
        writes.append(len(args[2]))
        return save(*args, **kwargs)
    monkeypatch.setattr(module, "save_transcript", recording)
    complete = pipeline.complete_frame
    def interrupted(*args):
        if recognizer.ocr_calls == 2:
            if interruption == "stop":
                client.post(f"/api/v1/tasks/{task_id}/stop")
            elif interruption == "commit":
                engine = app.state.sessions.kw["bind"]
                def fail_commit(connection):
                    raise RuntimeError("simulated commit failure")
                event.listen(engine, "commit", fail_commit)
                try:
                    return complete(*args)
                finally:
                    event.remove(engine, "commit", fail_commit)
        return complete(*args)
    if interruption:
        monkeypatch.setattr(pipeline, "complete_frame", interrupted)
    pipeline.run(task_id)
    assert len(writes) == 2
    if interruption:
        assert recognizer.ocr_calls == 2
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == ("paused" if interruption == "stop" else "failed")
        action = "resume" if interruption == "stop" else "retry"
        monkeypatch.setattr(pipeline, "complete_frame", complete)
        client.post(f"/api/v1/tasks/{task_id}/{action}")
        pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert recognizer.ocr_calls == frames


def test_transcript_cleanup_does_not_replace_failure(client, app, video_file, monkeypatch):
    import app.videos.pipeline as module
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, FakeRecognizer, FakeModel)
    monkeypatch.setattr(pipeline, "complete_frame", lambda *args: (_ for _ in ()).throw(RuntimeError("original failure")))
    save, count = module.save_transcript, 0
    def broken(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("cleanup failure")
        return save(*args, **kwargs)
    monkeypatch.setattr(module, "save_transcript", broken)
    pipeline.run(task_id)
    with app.state.sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "failed"
        log = pipeline.get_settings().log_dir / task.error["log_file"].split("/")[-1]
        assert "original failure" in log.read_text(encoding="utf-8")
