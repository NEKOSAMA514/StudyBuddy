import asyncio
import json
from pathlib import Path
import time

import pytest

from app.analysis.checkpoints import checkpoint_path
from app.videos.detailed import DetailedVideoGeneration
from app.analysis.worker import Worker
from app.db import Task
from app.errors import AppError
from app.util.json_io import atomic_json
from .fakes import FakeModel


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_atomic_json_preserves_serialization_and_temp_name(tmp_path, monkeypatch, ensure_ascii):
    path = tmp_path / "state.json"
    value = {"状态": "已完成", "items": [1, None]}
    replacements = []
    original = Path.replace
    def replace(source, target):
        replacements.append((source, target))
        return original(source, target)
    monkeypatch.setattr(Path, "replace", replace)
    atomic_json(path, value, ensure_ascii=ensure_ascii)
    assert path.read_bytes() == json.dumps(value, ensure_ascii=ensure_ascii).encode("utf-8")
    assert replacements == [(path.with_suffix(".tmp"), path)]
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("operation", ["write_text", "replace"])
def test_model_download_write_failure_keeps_published_memory_and_releases_lock(tmp_path, monkeypatch, operation):
    from app.config import Settings
    from app.diagnostics import Diagnostics
    from app.recognition.local_models import ModelDownloads
    settings = Settings(data_dir=tmp_path, model_dir=tmp_path / "models", log_dir=tmp_path / "log")
    downloads = ModelDownloads(settings, Diagnostics(settings.log_dir, lambda: ""))
    downloads.update("test-model", status="downloading", progress=1)
    path = downloads.directory / "test-model.json"
    previous = path.read_bytes()
    original = getattr(Path, operation)
    def fail(source, *args, **kwargs):
        if source == path.with_suffix(".tmp"):
            assert downloads.states["test-model"]["progress"] == 2
            assert downloads.lock.locked()
            raise OSError("simulated state write failure")
        return original(source, *args, **kwargs)
    monkeypatch.setattr(Path, operation, fail)
    with pytest.raises(OSError, match="simulated state write failure"):
        downloads.update("test-model", progress=2)
    assert path.read_bytes() == previous and downloads.states["test-model"]["progress"] == 2
    assert downloads.lock.acquire(blocking=False)
    downloads.lock.release()


def folder_for(generation):
    video_id, _, pipeline, *_ = generation
    return pipeline.get_settings().data_dir / "videos" / video_id


def checkpoints(folder, task_id=None):
    # 只核对应用生成的标准名称；忽略目录中短暂出现的非标准大写 .JSON.tmp 文件。
    return [p for p in folder.iterdir() if p.name.startswith("blocks-") and
            (task_id is None or f"-{task_id}-" in p.name)]


def test_success_marks_only_owned_files_after_commit_then_clears(client, generation, monkeypatch):
    _, task_id, pipeline, *_ = generation
    folder = folder_for(generation)
    other = checkpoint_path(folder, "other-task", 4, "outline", "a" * 16)
    other.write_bytes(b"unfinished")
    legacy = folder / f'blocks-v4-overview-{"b" * 16}.json'
    legacy.write_bytes(b"unowned history")
    other_folder = folder.parent / "other-video"
    other_folder.mkdir()
    unrelated = checkpoint_path(other_folder, task_id, 4, "outline", "a" * 16)
    unrelated.write_bytes(b"other video")
    original_replace, original_unlink = Path.replace, Path.unlink
    renamed, deleted = [], []

    def rename(path, target):
        if f"-{task_id}-recent" in path.name and "-success" in Path(target).name:
            with pipeline.sessions() as session:
                task = session.get(Task, task_id)
                assert task.status == "succeeded" and task.result["complete"]
                assert task.config["checkpoint_cleanup_pending"]
            assert not deleted
            renamed.append(path.name)
        return original_replace(path, target)

    def remove(path, **kwargs):
        if f"-{task_id}-success" in path.name:
            assert all("-recent" not in p.name for p in checkpoints(folder, task_id))
            deleted.append(path.name)
        return original_unlink(path, **kwargs)

    monkeypatch.setattr(Path, "replace", rename)
    monkeypatch.setattr(Path, "unlink", remove)
    pipeline.run(task_id)
    assert renamed and len(deleted) == len(renamed)
    assert not checkpoints(folder, task_id)
    assert other.read_bytes() == b"unfinished" and legacy.read_bytes() == b"unowned history"
    assert unrelated.read_bytes() == b"other video"
    assert (folder / "audio.wav").exists()
    assert list(folder.glob("original.*")) and list(folder.glob("frame-*.jpg"))
    assert list(folder.glob("speech-*.json")) and list(folder.glob("ocr-*.json"))
    assert list((folder / "transcripts").rglob("transcript.txt"))
    notes = client.get(f"/api/v1/analyses/{task_id}")
    assert notes.status_code == 200
    source = notes.json()["chapters"][0]["evidence_ids"][0]
    assert client.get(f"/api/v1/evidence/{source}").status_code == 200
    assert client.get(f"/api/v1/analyses/{task_id}/notes?format=md").status_code == 200
    with pipeline.sessions() as session:
        assert "checkpoint_cleanup_pending" not in session.get(Task, task_id).config


@pytest.mark.parametrize("failure", ["rename", "delete"])
def test_cleanup_failure_keeps_success_and_durable_retry(client, generation, monkeypatch, failure):
    video_id, task_id, pipeline, *_ = generation
    folder = folder_for(generation)
    original = getattr(Path, "replace" if failure == "rename" else "unlink")
    name = "replace" if failure == "rename" else "unlink"
    count = 0

    def fail(path, *args, **kwargs):
        nonlocal count
        if path.name.startswith("blocks-") and f"-{task_id}-" in path.name and (
                failure == "delete" or str(args[0]).endswith("success.json")):
            count += 1
            if count == 2:
                raise PermissionError("simulated locked checkpoint")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, name, fail)
        pipeline.run(task_id)
    with pipeline.sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "succeeded" and task.error is None and task.config["checkpoint_cleanup_pending"]
        completed_at = task.updated_at
    before = client.get(f"/api/v1/analyses/{task_id}").json()
    versions = client.get(f"/api/v1/videos/{video_id}/analyses").json()
    assert any("-success" in p.name for p in checkpoints(folder, task_id))
    if failure == "rename":
        assert any("-recent" in p.name for p in checkpoints(folder, task_id))
    assert list(pipeline.get_settings().log_dir.glob("*.log"))
    pipeline.retry_checkpoint_cleanup()
    assert not checkpoints(folder, task_id)
    assert client.get(f"/api/v1/analyses/{task_id}").json() == before
    assert client.get(f"/api/v1/videos/{video_id}/analyses").json() == versions
    assert client.get(f"/api/v1/analyses/{task_id}/notes").status_code == 200
    with pipeline.sessions() as session:
        task = session.get(Task, task_id)
        assert task.updated_at == completed_at and "checkpoint_cleanup_pending" not in task.config


def test_worker_recovers_cleanup_after_commit_even_if_result_retired(client, generation, monkeypatch):
    video_id, first, pipeline, _, calls, _ = generation
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "cleanup_checkpoints", lambda task_id: None)
        pipeline.run(first)
        for _ in range(2):
            task_id = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
            pipeline.run(task_id)
    with pipeline.sessions() as session:
        task = session.get(Task, first)
        assert task.result is None and task.config["checkpoint_cleanup_pending"]
        completed_at = task.updated_at
    folder = folder_for(generation)
    assert checkpoints(folder)
    model_calls = dict(calls)
    worker = Worker(pipeline.sessions, pipeline.diagnostics, {"video": (Task, pipeline)})
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not checkpoints(folder):
                break
            time.sleep(0.02)
        assert not checkpoints(folder)
        assert calls == model_calls
        with pipeline.sessions() as session:
            assert session.get(Task, first).updated_at == completed_at
    finally:
        worker.stop()


def test_restart_preserves_interrupted_task_checkpoints(client, generation):
    _, task_id, pipeline, *_ = generation
    folder = folder_for(generation)
    path = checkpoint_path(folder, task_id, 4, "outline", "d" * 16)
    path.write_bytes(b"unfinished outline")
    with pipeline.sessions.begin() as session:
        session.get(Task, task_id).status = "running"
    worker = Worker(pipeline.sessions, pipeline.diagnostics, {"video": (Task, pipeline)})
    worker.start()
    try:
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "interrupted"
        pipeline.retry_checkpoint_cleanup()
        assert path.read_bytes() == b"unfinished outline"
    finally:
        worker.stop()


@pytest.mark.parametrize("stop", [False, True])
def test_failure_or_pause_keeps_recent_until_resumed_success(client, generation, stop):
    _, task_id, pipeline, _, _, model_type = generation
    class FailingModel(model_type):
        async def generate(self, operation, payload, validate):
            if operation == "overview":
                if stop:
                    from app.errors import TaskPaused
                    raise TaskPaused()
                raise AppError("model_access_denied", "simulated failure")
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = FailingModel
    pipeline.run(task_id)
    folder = folder_for(generation)
    files = {p: p.read_bytes() for p in checkpoints(folder)}
    assert files and all("-recent" in p.name for p in files)
    assert client.get(f"/api/v1/analyses/{task_id}").json()["complete"] is False
    pipeline.retry_checkpoint_cleanup()
    assert all(p.read_bytes() == value for p, value in files.items())
    assert client.post(f"/api/v1/tasks/{task_id}/{'resume' if stop else 'retry'}").status_code == 202
    pipeline.model_factory = model_type
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert not checkpoints(folder)


def test_failed_success_transaction_never_marks_or_deletes(client, generation, monkeypatch):
    _, task_id, pipeline, *_ = generation
    def rollback(session, video_id):
        session.flush()
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr("app.videos.pipeline.retain_two_notes", rollback)
    pipeline.run(task_id)
    with pipeline.sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "failed" and "checkpoint_cleanup_pending" not in task.config
    files = checkpoints(folder_for(generation))
    assert files and all("-recent" in p.name for p in files)
    pipeline.retry_checkpoint_cleanup()
    assert all(p.exists() for p in files)


def test_exact_legacy_checkpoints_resume_without_model_calls(tmp_path):
    from app.config import Settings
    settings = Settings()
    evidence = [{"id": "source", "kind": "speech", "start_seconds": 0, "end_seconds": 1, "text": "原文"}]
    def flow(model_type=FakeModel):
        model = model_type(settings)
        return DetailedVideoGeneration("legacy-task", model, settings, tmp_path, evidence, 2,
                              asyncio.Event(), lambda: None, lambda _: None, {})
    expected = asyncio.run(flow().run())
    for p in checkpoints(tmp_path):
        p.rename(p.with_name(p.name.replace("-legacy-task-recent", "")))
    legacy_split = tmp_path / f'blocks-v4-chapters-{"a" * 16}.split.json'
    legacy_split.write_text('{"reason":"split"}', encoding="utf-8")
    path = checkpoint_path(tmp_path, "legacy-task", 4, "chapters", "a" * 16)
    assert path.with_suffix(".split.json").is_file() and not legacy_split.exists()
    class NoCalls(FakeModel):
        async def generate(self, *args):
            pytest.fail("compatible legacy checkpoints should resume without model calls")
    assert asyncio.run(flow(NoCalls).run()) == expected


def test_cleanup_includes_split_and_temp_files_but_keeps_unknown_files(tmp_path):
    from app.analysis.checkpoints import clear_checkpoints
    path = checkpoint_path(tmp_path, "task", 4, "chapters", "c" * 16)
    for suffix in (".json", ".split.json", ".tmp", ".split.tmp"):
        path.with_suffix(suffix).write_bytes(b"checkpoint")
    unknown = path.with_suffix(".jpg")
    unknown.write_bytes(b"unrelated")
    clear_checkpoints(tmp_path, "task")
    assert list(tmp_path.iterdir()) == [unknown]
