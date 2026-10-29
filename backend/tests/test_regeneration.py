import pytest
from copy import deepcopy
from sqlalchemy import select
from app.db import Video, Task, Evidence
from app.errors import AppError
from .conftest import upload, stored_task_config


def test_new_task_regenerates_all_notes_but_reuses_recognition(client, generation):
    video_id, first, pipeline, recognizer, calls, _ = generation
    pipeline.run(first)
    old_notes = client.get(f"/api/v1/analyses/{first}").json()
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    assert second != first
    pipeline.run(second)
    assert calls == {"outline": 2, "chapters": 2, "overview": 2}
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60
    assert client.get(f"/api/v1/analyses/{second}").json()["title"] != old_notes["title"]
    assert client.get(f"/api/v1/analyses/{first}").json() == old_notes


def test_third_success_retires_only_oldest_notes_and_keeps_sources(client, app, generation):
    video_id, first, pipeline, recognizer, _, _ = generation
    pipeline.run(first)
    with app.state.sessions() as session:
        task = session.get(Task, first)
        original = (task.status, task.updated_at, deepcopy(task.timings), deepcopy(task.config))
        source_ids = list(session.scalars(select(Evidence.id).where(Evidence.video_id == video_id)))
    folder = pipeline.get_settings().data_dir / "videos" / video_id
    files = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(second)
    third = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    assert client.get(f"/api/v1/analyses/{first}").status_code == 200
    pipeline.run(third)
    versions = client.get(f"/api/v1/videos/{video_id}/analyses").json()
    assert [task["id"] for task in versions] == [third, second]
    assert all(task["notes_state"] == "ready" and "result" not in task for task in versions)
    for task in versions:
        assert set(task["config"]) <= {"extract_version", "outline_preview", "transcript_source"}
        assert client.get(f'/api/v1/analyses/{task["id"]}').status_code == 200
        assert client.get(f'/api/v1/analyses/{task["id"]}/notes?format=md').status_code == 200
    assert client.get(f"/api/v1/analyses/{first}").status_code == 409
    assert client.get(f"/api/v1/analyses/{first}/notes").status_code == 409
    assert client.get(f"/api/v1/tasks/{first}").json()["notes_state"] == "none"
    with app.state.sessions() as session:
        task = session.get(Task, first)
        assert task.result is None and "outline_preview" not in task.config
        assert (task.status, task.updated_at, task.timings) == original[:3]
        assert task.config == {key: value for key, value in original[3].items() if key != "outline_preview"}
        assert list(session.scalars(select(Evidence.id).where(Evidence.video_id == video_id))) == source_ids
    assert all(path.read_bytes() == content for path, content in files.items())
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60


@pytest.mark.parametrize("status", ["running", "failed", "paused"])
def test_partial_or_unsuccessful_task_does_not_displace_successful_notes(client, app, generation, status):
    video_id, first, pipeline, _, _, _ = generation
    pipeline.run(first)
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(second)
    with app.state.sessions.begin() as session:
        partial = {**deepcopy(session.get(Task, first).result), "complete": False,
                   "covered_until_seconds": 2}
        session.add(Task(id="draft", video_id=video_id, status=status, stage="segments", config={}, result=partial))
    versions = client.get(f"/api/v1/videos/{video_id}/analyses").json()
    assert [task["id"] for task in versions] == [second, first]
    assert client.get("/api/v1/analyses/draft").status_code == 200
    assert client.get(f"/api/v1/analyses/{first}").status_code == 200


def test_existing_history_waits_for_next_success_and_other_videos_are_untouched(client, app, generation):
    video_id, first, pipeline, _, _, _ = generation
    pipeline.run(first)
    with app.state.sessions.begin() as session:
        original = session.get(Task, first)
        session.add(Video(id="other", fingerprint="other", filename="other.mp4", size_bytes=1,
                          duration=2, video_codec="h264"))
        session.flush()
        for index in range(3):
            for video in (video_id, "other"):
                session.add(Task(id=f"{video}-old-{index}", video_id=video, status="succeeded", stage="completed",
                                 config=deepcopy(original.config), result=deepcopy(original.result),
                                 created_at=f"2020-01-0{index + 1}", updated_at=f"2020-01-0{index + 1}"))
    assert len(client.get(f"/api/v1/videos/{video_id}/analyses").json()) == 2
    with app.state.sessions() as session:
        assert sum(task.result is not None for task in session.scalars(select(Task).where(Task.video_id == video_id))) == 4
    next_id = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    assert client.get(f"/api/v1/analyses/{video_id}-old-0").status_code == 200
    pipeline.run(next_id)
    with app.state.sessions() as session:
        assert sum(task.result is not None for task in session.scalars(select(Task).where(Task.video_id == video_id))) == 2
        assert all(task.result is not None for task in session.scalars(select(Task).where(Task.video_id == "other")))


def test_success_and_retirement_roll_back_together(client, app, generation, monkeypatch):
    from app.videos.notes import retain_two_notes
    video_id, first, pipeline, _, _, _ = generation
    pipeline.run(first)
    with app.state.sessions.begin() as session:
        task = session.get(Task, first)
        result, config = deepcopy(task.result), deepcopy(task.config)
        session.add(Task(id="previous", video_id=video_id, status="succeeded", stage="completed",
                         config=config, result=result, updated_at="2020-01-01"))
        session.add(Task(id="new", video_id=video_id, status="running", stage="finalizing", config=config))
    def fail_after_retirement(session, video_id):
        retain_two_notes(session, video_id)
        session.flush()
        raise RuntimeError("simulated transaction failure")
    monkeypatch.setattr("app.videos.pipeline.retain_two_notes", fail_after_retirement)
    with pytest.raises(RuntimeError, match="transaction failure"):
        pipeline.update("new", status="succeeded", result=result)
    with app.state.sessions() as session:
        assert session.get(Task, "new").status == "running" and session.get(Task, "new").result is None
        assert session.get(Task, "previous").result == result
        assert session.get(Task, "previous").config == config
        assert session.get(Task, first).result == result


def test_resumed_old_task_is_ranked_by_completion_time(client, app, generation):
    video_id, first, pipeline, _, _, _ = generation
    pipeline.run(first)
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(second)
    with app.state.sessions.begin() as session:
        session.add(Task(id="old-paused", video_id=video_id, status="paused", stage="queued",
                         config=deepcopy(session.get(Task, first).config), created_at="2020-01-01"))
    assert client.post("/api/v1/tasks/old-paused/resume").status_code == 202
    pipeline.run("old-paused")
    assert [task["id"] for task in client.get(f"/api/v1/videos/{video_id}/analyses").json()] == ["old-paused", second]
    assert client.get(f"/api/v1/analyses/{first}").status_code == 409


def test_version_list_empty_missing_video_and_legacy_results(client, app, video_file):
    video_id = upload(client, video_file)["id"]
    assert client.get(f"/api/v1/videos/{video_id}/analyses").json() == []
    assert client.get("/api/v1/videos/missing/analyses").status_code == 404
    with app.state.sessions.begin() as session:
        session.add(Task(id="legacy", video_id=video_id, status="succeeded", stage="completed", config={},
                         result={"format_version": 3, "chapters": [{"title": "旧格式"}]}))
    assert client.get(f"/api/v1/videos/{video_id}/analyses").json() == []


def test_versions_keep_their_own_recognition_sources_and_downloads(client, app, generation, monkeypatch):
    from .support import fake_files
    video_id, first, pipeline, _, _, _ = generation
    pipeline.run(first)
    old_notes = client.get(f"/api/v1/analyses/{first}").json()
    old_sources = client.get(f"/api/v1/videos/{video_id}/evidence?task_id={first}").json()
    client.get("/api/v1/settings/recognition")
    fake_files(pipeline.get_settings(), "whisper-base")
    assert client.put("/api/v1/settings/recognition", json={"asr_option": "whisper-base", "ocr_option": "rapidocr", "language": "auto"}).status_code == 200
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(second)
    assert stored_task_config(app, second)["asr_engine"] == "whisper"
    new_notes = client.get(f"/api/v1/analyses/{second}").json()
    assert set(old_notes["evidence_ids"]).isdisjoint(new_notes["evidence_ids"])
    assert client.get(f"/api/v1/analyses/{first}").json() == old_notes
    assert client.get(f"/api/v1/videos/{video_id}/evidence?task_id={first}").json() == old_sources
    for task_id, notes in ((first, old_notes), (second, new_notes)):
        sources = client.get(f"/api/v1/videos/{video_id}/evidence?task_id={task_id}").json()
        assert {source["id"] for source in sources} == set(notes["evidence_ids"])
        assert client.get(f"/api/v1/analyses/{task_id}/notes?format=md").status_code == 200
        assert client.get(f"/api/v1/videos/{video_id}/transcript?task_id={task_id}").status_code == 200
        frame = next(source for source in sources if source["kind"] == "frame")
        assert client.get(f'/api/v1/evidence/{frame["id"]}/image').status_code == 200


def test_retry_reuses_this_tasks_completed_sections(client, generation, monkeypatch):
    _, task_id, pipeline, recognizer, calls, Model = generation
    generate = Model.generate

    async def fail(self, operation, payload, validate):
        if operation == "overview":
            raise AppError("model_access_denied", "test model failure")
        return await generate(self, operation, payload, validate)

    monkeypatch.setattr(Model, "generate", fail)
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "failed"
    monkeypatch.setattr(Model, "generate", generate)
    assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 202
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert calls == {"outline": 1, "chapters": 1, "overview": 1}
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60


@pytest.mark.parametrize("pause_at", ["outline", "chapters", "overview"])
def test_resume_reuses_this_tasks_notes_checkpoints(client, generation, monkeypatch, pause_at):
    _, task_id, pipeline, recognizer, calls, Model = generation
    method = Model.generate

    async def stop_after_generation(self, operation, payload, validate):
        result = await method(self, operation, payload, validate)
        if operation != pause_at:
            return result
        assert client.post(f"/api/v1/tasks/{task_id}/stop").json()["status"] == "stopping"
        return result

    monkeypatch.setattr(Model, "generate", stop_after_generation)
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "paused"
    monkeypatch.setattr(Model, "generate", method)
    assert client.post(f"/api/v1/tasks/{task_id}/resume").status_code == 202
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert calls == {"outline": 1, "chapters": 1, "overview": 1}
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60
