from app.analysis.content_types import CONTENT_TYPES
import asyncio
import json
from dataclasses import replace
import pytest
from sqlalchemy import select
from app.db import Evidence, Task, Video
from app.errors import AppError
from app.videos.pipeline import Pipeline
from app.videos.schemas import ground_timestamps
from app.videos.serialization import task_progress
from .conftest import upload, stored_task_config
from .fakes import FakeRecognizer, FakeModel


def prepare(client, app, video_file):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    recognizer = FakeRecognizer(app.state.pipeline.get_settings())
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    return video, task_id, pipeline, recognizer


def test_pipeline_grounded_result_and_checkpoint(client, app, video_file):
    video, task_id, pipeline, recognizer = prepare(client, app, video_file)
    pipeline.run(task_id)
    result = client.get(f"/api/v1/analyses/{task_id}").json()
    assert result["title"] == "TCP 慢启动与拥塞窗口增长"
    assert result["tags"] == ["TCP", "慢启动", "拥塞窗口"]
    assert result["basis"] == ["frame"]
    assert result["chapters"][0]["start_seconds"] == 0
    identifier = result["chapters"][0]["evidence_ids"][0]
    evidence = client.get(f"/api/v1/evidence/{identifier}").json()
    assert evidence["video_id"] == video["id"]
    assert client.get(f"/api/v1/evidence/{identifier}/image").status_code == 200
    with app.state.sessions.begin() as session:
        session.add(Evidence(id="old-parse", video_id=video["id"], kind="speech", start_seconds=0, end_seconds=1, text="旧解析结果"))
    scoped = client.get(f'/api/v1/videos/{video["id"]}/evidence', params={"task_id": task_id}).json()
    assert all(e["id"] != "old-parse" for e in scoped)
    new_task = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(new_task)
    assert recognizer.ocr_calls == 60
    assert client.get(f"/api/v1/tasks/{new_task}").json()["status"] == "succeeded"


def test_model_failure_keeps_evidence_and_retry_reuses_it(client, app, video_file):
    video, task_id, pipeline, recognizer = prepare(client, app, video_file)
    class FailedModel(FakeModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                raise AppError("model_access_denied", "免费额度已用尽")
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = FailedModel
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["error"]["code"] == "model_access_denied"
    with app.state.sessions() as session:
        assert session.scalar(select(Evidence)) is not None
    from app.recognition.extraction import extraction_signature
    config = stored_task_config(app, task_id)
    folder = pipeline.get_settings().data_dir / "videos" / video["id"] / "transcripts" / extraction_signature(config)
    assert (folder / "transcript.txt").is_file()
    assert "慢启动" in (folder / "transcript.txt").read_text(encoding="utf-8")
    response = client.get(f'/api/v1/videos/{video["id"]}/transcript', params={"task_id": task_id, "format": "json"})
    assert response.status_code == 200
    assert response.json()["frames"][0]["text"] == "TCP 慢启动：拥塞窗口增长"
    client.post(f"/api/v1/tasks/{task_id}/retry")
    pipeline.model_factory = FakeModel
    pipeline.run(task_id)
    assert recognizer.ocr_calls == 60
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"


def test_old_task_transcript_export_scopes_version_and_checks_video(client, app, video_file):
    video, task_id, pipeline, _ = prepare(client, app, video_file)
    pipeline.run(task_id)
    from app.recognition.extraction import extraction_signature
    config = stored_task_config(app, task_id)
    folder = pipeline.get_settings().data_dir / "videos" / video["id"] / "transcripts" / extraction_signature(config)
    for file in folder.iterdir():
        file.unlink()
    with app.state.sessions.begin() as session:
        session.add(Evidence(id="old-version", video_id=video["id"], kind="speech", start_seconds=0, end_seconds=1, text="旧版本不应混入"))
    response = client.get(f'/api/v1/videos/{video["id"]}/transcript', params={"task_id": task_id})
    assert response.status_code == 200
    assert "慢启动" in response.text and "旧版本不应混入" not in response.text
    assert f"视频标题：{video['filename'].rsplit('.', 1)[0]}" in response.text
    exported = client.get(f'/api/v1/videos/{video["id"]}/transcript', params={"task_id": task_id, "format": "json"}).json()
    assert exported["video_title"] == video["filename"].rsplit(".", 1)[0]
    assert exported["source_filename"] == video["filename"] and exported["duration_seconds"] == video["duration"]
    assert "attachment" in response.headers["content-disposition"]
    assert not list(folder.iterdir())  # 下载按需构建，不改写检查点文件。
    from app.db import Video
    with app.state.sessions.begin() as session:
        session.add(Video(id="other-video", fingerprint="other", filename="other.mp4", size_bytes=1, duration=2, video_codec="h264"))
    assert client.get('/api/v1/videos/other-video/transcript', params={"task_id": task_id}).status_code == 404
    assert client.get(f'/api/v1/videos/{video["id"]}/transcript', params={"task_id": task_id, "format": "exe"}).status_code == 422


def test_no_evidence_never_calls_model(client, app, video_file):
    _, task_id, pipeline, recognizer = prepare(client, app, video_file)
    recognizer.ocr = lambda path: ("", None)
    pipeline.model_factory = lambda settings: pytest.fail("无证据不允许调用模型")
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["error"]["code"] == "no_evidence"


def test_partial_ocr_failure_preserves_audio(client, app, video_file):
    video, task_id, pipeline, recognizer = prepare(client, app, video_file)
    from app.db import Video
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    import unittest.mock
    def ocr_error(path):
        raise RuntimeError("OCR unavailable")
    recognizer.ocr = ocr_error
    with unittest.mock.patch("app.videos.pipeline.extract_audio", lambda a, b, media_info=None: b.write_bytes(b"fake-audio")):
        pipeline.run(task_id)
    result = client.get(f"/api/v1/analyses/{task_id}").json()
    assert result["basis"] == ["speech"]
    assert result["warnings"]


def test_grounding_rejects_cross_video_and_out_of_range():
    with pytest.raises(AppError):
        ground_timestamps(["foreign"], {}, 10)
    with pytest.raises(AppError):
        ground_timestamps(["foreign"], {"foreign": {"start_seconds": 12, "end_seconds": 15}}, 10)


def test_asr_failure_can_use_ocr_with_warning(client, app, video_file):
    video, task_id, pipeline, recognizer = prepare(client, app, video_file)
    from app.db import Video
    import unittest.mock
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    def broken_asr(path):
        raise RuntimeError("ASR unavailable")
    recognizer.transcribe = broken_asr
    with unittest.mock.patch("app.videos.pipeline.extract_audio", lambda a, b, media_info=None: b.write_bytes(b"fake-audio")):
        pipeline.run(task_id)
    result = client.get(f"/api/v1/analyses/{task_id}").json()
    assert result["basis"] == ["frame"]
    assert any("语音识别未完成" in warning for warning in result["warnings"])


def test_queued_tasks_resume_on_worker_start(client, app, video_file):
    _, task_id, pipeline, _ = prepare(client, app, video_file)
    from app.analysis.worker import Worker
    import time
    worker = Worker(pipeline.sessions, pipeline.diagnostics, {"video": (Task, pipeline)})
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/tasks/{task_id}").json()["status"]
            if state in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        assert state == "succeeded"
    finally:
        worker.stop()


@pytest.mark.parametrize("stage,status,percent,completed", [
    ("queued", "queued", 0, 0), ("transcribing", "running", 16, 1),
    ("ocr:25/50", "running", 41, 2), ("planning", "running", 50, 3), ("summarizing:2/4", "running", 75, 4),
    ("finalizing", "running", 83, 5), ("finalizing", "failed", 83, 5),
    ("completed", "succeeded", 100, 6),
])
def test_progress_reflects_steps_and_never_completes_on_failure(stage, status, percent, completed):
    from types import SimpleNamespace
    progress = task_progress(SimpleNamespace(stage=stage, status=status, config={}))
    assert progress["percent"] == percent
    assert progress["completed_steps"] == completed


def test_prompt_change_invalidates_summary_but_reuses_recognition(client, app, video_file, monkeypatch):
    _, task_id, pipeline, recognizer = prepare(client, app, video_file)
    calls = []
    class RecordingModel(FakeModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters":
                calls.append(1)
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = RecordingModel
    pipeline.run(task_id)
    video_id = client.get(f"/api/v1/tasks/{task_id}").json()["video_id"]
    second = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(second)
    assert len(calls) == 2
    from dataclasses import replace
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "请详细解释步骤。", "note_style": "detailed"})
    third = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(third)
    assert len(calls) == 3
    assert recognizer.ocr_calls == 60
    task = client.get(f"/api/v1/tasks/{third}").json()
    assert task["progress"]["percent"] == 100
    assert stored_task_config(app, task["id"] )["summary_prompt"] == "请详细解释步骤。"
    client.put("/api/v1/settings/note-style", json={"summary_prompt": "", "note_style": "detailed"})
    fourth = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(fourth)
    assert len(calls) == 4  # 新任务即使恢复相同偏好，也必须重新总结。
    assert stored_task_config(app, fourth)["summary_prompt"] == ""
    monkeypatch.setattr("app.videos.pipeline.PROMPT_VERSION", "test-next-system-rules")
    monkeypatch.setattr("app.videos.versions.PROMPT_VERSION", "test-next-system-rules")
    monkeypatch.setattr("app.videos.analysis_service.PROMPT_VERSION", "test-next-system-rules")
    fifth = client.post(f"/api/v1/videos/{video_id}/analyses").json()["task_id"]
    pipeline.run(fifth)
    assert len(calls) == 5  # 固定系统规则版本变化必须重新总结。
    assert recognizer.ocr_calls == 60
    assert client.get(f"/api/v1/analyses/{fifth}").json()["prompt_version"] == "test-next-system-rules"


@pytest.fixture
def parallel_generation(client, app, video_file, monkeypatch):
    video, task_id, pipeline, _ = prepare(client, app, video_file)
    app.state.worker.register("video", Task, pipeline)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
        task = session.get(Task, task_id)
        task.config = {**task.config, "llm_section_concurrency": 4}
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, target, media_info=None: target.write_bytes(b"fake-audio"))
    state = {"started": [], "finished": [], "finalized": [], "cancelled": [], "active": 0, "peak": 0,
             "action": None, "progress": [], "closed": 0}
    class Recognizer(FakeRecognizer):
        def transcribe(self, path):
            self.asr_calls += 1
            return [{"start_seconds": index / 10, "end_seconds": (index + 1) / 10,
                     "text": str(index)} for index in range(6)]
    class Model(FakeModel):
        def input_characters(self, operation, payload):
            if operation == "chapters" and len(payload["chapters"]) > 1:
                return 1000000
            return super().input_characters(operation, payload)
        async def __aexit__(self, *exc):
            state["closed"] += 1
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.calls += 1
                starts = []
                for anchor in payload["anchors"]:
                    if not starts or anchor["start_seconds"] > starts[-1]["start_seconds"]:
                        starts.append(anchor)
                value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": str(index), "core_question": "问题", "topics": ["主题"],
                         "start_anchor_id": item["id"]} for index, item in enumerate(starts)],
                         "video_summary": "视频有六个主题。"}
                validate(value)
                return value
            if operation == "overview":
                assert state["active"] == 0 and "evidence" not in payload
                state["finalized"].append(payload["video_summaries"])
                return await super().generate(operation, payload, validate)
            self.calls += 1
            chapter = payload["chapters"][0]
            index = int(chapter["title"])
            assert not (state.get("halted") and state["action"]), "stopped scheduling must not start more calls"
            state["started"].append(index)
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                delay = (0.01 if index == 0 else 0.04 if index == 3 else 30) if state["action"] else (6 - index) * 0.02
                await asyncio.sleep(delay)
                if index == 3 and state["action"] == "stop":
                    state["halted"] = True
                    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 202
                if index == 3 and state["action"] == "fail":
                    state["halted"] = True
                    raise AppError("invalid_model_output", "测试章节失败")
                state["finished"].append(index)
                speech = next(item for item in chapter["primary"] if item["kind"] == "speech")
                value = {"chapters": [{'chapter_id': chapter['chapter_id'], 'summary': '章节摘要', "markdown": '正文', "issues": []}]}
                validate(value)
                return value
            except asyncio.CancelledError:
                state["cancelled"].append(index)
                raise
            finally:
                state["active"] -= 1
    pipeline.recognizer_factory, pipeline.model_factory = Recognizer, Model
    update = pipeline.update
    def record_progress(identifier, **values):
        if values.get("stage", "").startswith("summarizing:"):
            state["progress"].append(values["stage"])
        return update(identifier, **values)
    pipeline.update = record_progress
    return video, task_id, pipeline, state


@pytest.mark.parametrize("concurrency", [1, 3, 4])
def test_parallel_chapters_bound_and_order_before_overview(client, parallel_generation, concurrency):
    _, task_id, pipeline, state = parallel_generation
    with pipeline.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.config = {**task.config, "llm_section_concurrency": concurrency}
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert state["peak"] == concurrency and state["active"] == 0
    assert state["finalized"] == [["视频有六个主题。"]]
    assert [block["title"] for block in client.get(f"/api/v1/analyses/{task['id']}").json()["chapters"]] == list(map(str, range(6)))
    assert state["progress"] == [f"summarizing:{index}/6" for index in range(7)]
    assert client.get(f"/api/v1/analyses/{task['id']}").json()["model_requests"] == 8
    assert all(stage in task["timings"] for stage in ("planning", "summarizing", "finalizing"))
    assert state["closed"] == 1


@pytest.mark.parametrize("action,status,cached", [("stop", "paused", 2), ("fail", "failed", 1)])
def test_stop_or_failure_cancels_inflight_and_reuses_completed(client, parallel_generation, action, status, cached):
    import time
    video, task_id, pipeline, state = parallel_generation
    state["action"] = action
    tick = time.monotonic()
    pipeline.run(task_id)
    assert time.monotonic() - tick < 2
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    folder = pipeline.get_settings().data_dir / "videos" / video["id"]
    assert task["status"] == status and "result" not in task
    assert state["started"][:4] == [0, 1, 2, 3] and 5 not in state["started"]
    assert set(state["cancelled"]) == set(state["started"]) - {0, 3}
    started_before_resume = len(state["started"])
    assert state["active"] == 0 and state["finalized"] == [] and state["closed"] == 1
    assert len(list(folder.glob("blocks-v4-chapters-*.json"))) == cached
    state["action"] = None
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_section_concurrency=1): snapshot
    assert client.post(f"/api/v1/tasks/{task_id}/{'resume' if action == 'stop' else 'retry'}").status_code == 202
    pipeline.run(task_id)
    current = client.get(f"/api/v1/tasks/{task_id}").json()
    assert current["status"] == "succeeded"
    assert state["started"][started_before_resume:] == ([1, 2, 4, 5] if action == "stop" else [1, 2, 3, 4, 5])
    assert client.get(f"/api/v1/analyses/{current['id']}").json()["model_requests"] == 7 - cached
    assert state["closed"] == 2


def pause_before_final_commit(pipeline, monkeypatch):
    from app.errors import TaskPaused
    update = pipeline.update
    pending = True
    def pause_once(task_id, **values):
        nonlocal pending
        if pending and values.get("status") == "succeeded":
            pending = False
            raise TaskPaused()
        return update(task_id, **values)
    monkeypatch.setattr(pipeline, "update", pause_once)


def test_cached_blocks_need_no_model_requests(client, parallel_generation, monkeypatch):
    _, task_id, pipeline, state = parallel_generation
    pause_before_final_commit(pipeline, monkeypatch)
    pipeline.run(task_id)
    original = client.get(f"/api/v1/analyses/{task_id}").json()
    with pipeline.sessions.begin() as session:
        session.get(Task, task_id).status = "queued"
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_section_concurrency=1): snapshot
    pipeline.run(task_id)
    current = client.get(f"/api/v1/tasks/{task_id}").json()
    assert current["status"] == "succeeded" and client.get(f"/api/v1/analyses/{current['id']}").json()["model_requests"] == 0
    assert client.get(f"/api/v1/analyses/{current['id']}").json()["chapters"] == original["chapters"]
    assert len(state["started"]) == 6 and len(state["finalized"]) == 1


def test_complete_segment_reuses_valid_body_despite_unused_chapter_cache(client, parallel_generation, monkeypatch):
    video, task_id, pipeline, state = parallel_generation
    pause_before_final_commit(pipeline, monkeypatch)
    pipeline.run(task_id)
    folder = pipeline.get_settings().data_dir / "videos" / video["id"]
    for cache in folder.glob("blocks-v4-chapter-*.json"):
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data["chapter_id"] == "C4":
            cache.write_text('{}', encoding="utf-8")
        else:
            cache.unlink()
    with pipeline.sessions.begin() as session:
        session.get(Task, task_id).status = "queued"
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert len(state["started"]) == 6 and len(state["finalized"]) == 1


def test_simulated_chapter_concurrency_benchmark(client, parallel_generation, tmp_path):
    video, task_id, pipeline, _ = parallel_generation
    report = {}
    for concurrency in (1, 4):
        if concurrency == 4:
            task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
        with pipeline.sessions.begin() as session:
            task = session.get(Task, task_id)
            task.config = {**task.config, "llm_section_concurrency": concurrency}
        pipeline.run(task_id)
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "succeeded" and client.get(f"/api/v1/analyses/{task['id']}").json()["model_requests"] == 8
        report[str(concurrency)] = task["timings"]
    (tmp_path / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def make_generation(tmp_path, evidence=None, model_type=FakeModel, budget=24000):
    from app.config import Settings
    from app.videos.detailed import DetailedVideoGeneration
    evidence = evidence or [{"id": "source", "kind": "speech", "start_seconds": 0,
                             "end_seconds": 1, "text": "原始证据需要核对。"}]
    model = model_type(Settings(llm_input_text_limit=budget))
    stages, timings = [], {}
    coordinator = DetailedVideoGeneration("task", model, model.settings, tmp_path, evidence, 20,
                                 asyncio.Event(), lambda: None, stages.append, timings)
    return coordinator, model, stages


def test_small_adjacent_chapters_batch_without_merging_structure(tmp_path, monkeypatch):
    monkeypatch.setattr("app.videos.detailed.MAX_BATCH_CHAPTERS", 5)
    calls = []
    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            calls.append((operation, payload))
            if operation == "outline":
                self.calls += 1
                value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": f"主题{i}", "core_question": "问题", "topics": ["子话题"],
                         "start_anchor_id": anchor["id"]} for i, anchor in enumerate(payload["anchors"])],
                         "video_summary": "各主题导览初稿。"}
                validate(value)
                return value
            return await super().generate(operation, payload, validate)
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i,
                 "end_seconds": i + 0.5, "text": f"主题{i}原文"} for i in range(7)]
    coordinator, _, stages = make_generation(tmp_path, evidence, Model)
    result = asyncio.run(coordinator.run())
    batches = [payload["chapters"] for operation, payload in calls if operation == "chapters"]
    assert list(map(len, batches)) == [5, 2]
    assert all(set(payload) == {"video_summary", "chapters", "content_type"} and payload["video_summary"] == "各主题导览初稿。"
               for operation, payload in calls if operation == "chapters")
    assert [chapter["topics"] for batch in batches for chapter in batch] == [["子话题"]] * 7
    assert [block["title"] for block in result["chapters"]] == [f"主题{i}" for i in range(7)]
    assert result["format_version"] == 4 and not {"sections", "points", "overall_summary"} & result.keys()
    assert len(calls) == 4 and stages[-1] == "finalizing"
    overview = calls[-1][1]
    assert set(overview) == {"video_summaries", "content_type"}
    assert overview["video_summaries"] == ["各主题导览初稿。"]


def test_primary_ownership_and_boundary_context_are_separate(tmp_path):
    evidence = [{"id": "s0", "kind": "speech", "start_seconds": 0, "end_seconds": 3, "text": "前章" * 400},
                {"id": "f0", "kind": "frame", "start_seconds": 1.5, "end_seconds": 1.5, "text": "画面条件"},
                {"id": "s1", "kind": "speech", "start_seconds": 3, "end_seconds": 4, "text": "后章" * 400},
                {"id": "f1", "kind": "frame", "start_seconds": 3, "end_seconds": 3, "text": "边界画面"}]
    coordinator, _, _ = make_generation(tmp_path, evidence)
    starts = [coordinator.anchors[0], next(anchor for anchor in coordinator.anchors if anchor["start_seconds"] == 3)]
    plans = [{"title": str(i), "core_question": "问题", "topics": ["主题"], "start_anchor_id": anchor["id"]}
             for i, anchor in enumerate(starts)]
    chapters = coordinator.assign({"content_type": CONTENT_TYPES["课程或教材"], "chapters": plans, "video_summary": "导览"})
    owners = [[coordinator.original[item["id"]]["id"] for item in chapter["primary"]] for chapter in chapters]
    assert owners == [["s0", "f0"], ["f1", "s1"]]
    assert chapters[0]["context"] == []
    assert chapters[1]["context"] == chapters[0]["primary"]
    assert chapters[1]["context"][0]["text"] == "前章" * 400
    coordinator.validate("chapters", {"chapters": [chapters[1]]},
        {"chapters": [{"chapter_id": "C2", "summary": "摘要", "markdown": "自由组织本章内容。", "issues": []}]})


def test_large_single_evidence_is_split_without_truncation_and_has_local_context(tmp_path):
    calls = []
    text = "开头标记" + "".join(f"第{i:04d}项材料需要完整保留。" for i in range(1600)) + "结尾标记"
    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            calls.append((operation, payload))
            if operation == "extract":
                self.calls += 1
                chapter = payload["chapter"]
                facts = [{"text": item['text'][:8] + item['text'][-8:]}
                         for item in chapter["primary"]]
                value = {"facts": facts, "issues": []}
                validate(value)
                return value
            if operation == "chapters":
                self.calls += 1
                value = {"chapters": [{'chapter_id': chapter['chapter_id'], 'summary': '整章摘要', "markdown": "\n\n".join([fact['text'] for fact in chapter['facts']]), "issues": []}
                         for chapter in payload["chapters"]]}
                validate(value)
                return value
            return await super().generate(operation, payload, validate)
    coordinator, model, _ = make_generation(tmp_path, [{"id": "long-source", "kind": "speech", "start_seconds": 0,
        "end_seconds": 10, "text": text}], Model, budget=12000)
    result = asyncio.run(coordinator.run())
    extracts = [payload for operation, payload in calls if operation == "extract"]
    assert len(extracts) > 1
    planned = False
    for operation, payload in calls:
        if operation == "outline":
            planned = True
        elif operation == "extract":
            assert set(payload) == ({"video_summary", "chapter", "content_type"} if planned else {"chapter"})
            if planned:
                assert payload["video_summary"] == "视频介绍 TCP 慢启动与拥塞窗口增长。"
    assert "".join(item["text"] for payload in extracts for item in payload["chapter"]["primary"]) == text
    # 原句从视频零秒开始，拆成文字块也不能把同一句的其他块冒充前一分钟。
    assert all(payload["chapter"]["context"] == [] for payload in extracts)
    assert all(model.input_characters(operation, payload) <= model.settings.llm_input_text_limit for operation, payload in calls)
    assert any("结尾标记" in block.get("markdown", "") for block in result["chapters"])
    assert all(identifier == "long-source" for block in result["chapters"] for identifier in block["evidence_ids"])
    assert coordinator.original["E1"]["text"] == text


@pytest.mark.parametrize("concurrency", [1, 4])
def test_extracted_chapter_starts_before_unrelated_chapters_finish(tmp_path, concurrency):
    merged = asyncio.Event()
    state = {"active": 0, "peak": 0, "finished": [], "extracts": 0}
    class Model(FakeModel):
        def input_characters(self, operation, payload):
            if operation == "chapters" and (len(payload["chapters"]) > 1 or
                    any(chapter["chapter_id"] == "C1" and chapter.get("primary") for chapter in payload["chapters"])):
                return 1000000
            return super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.calls += 1
                value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": str(i), "core_question": "问题", "topics": ["主题"],
                         "start_anchor_id": anchor["id"]} for i, anchor in enumerate(payload["anchors"])],
                         "video_summary": "所有章节的导览。"}
                validate(value)
                return value
            if operation == "overview":
                assert set(state["finished"]) == {f"C{i}" for i in range(1, 7)}
                assert state["active"] == 0
                return await super().generate(operation, payload, validate)
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                await asyncio.sleep(0)
                if operation == "extract":
                    state["extracts"] += 1
                else:
                    identifier = payload["chapters"][0]["chapter_id"]
                    if identifier == "C2":
                        await merged.wait()
                    if identifier == "C1":
                        assert "C2" not in state["finished"]
                        merged.set()
                value = await super().generate(operation, payload, validate)
                if operation == "chapters":
                    state["finished"].append(identifier)
                return value
            finally:
                state["active"] -= 1
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i,
                 "end_seconds": i + 0.5, "text": f"主题{i}原文"} for i in range(6)]
    coordinator, model, _ = make_generation(tmp_path, evidence, Model)
    coordinator.settings = model.settings = replace(model.settings, llm_section_concurrency=concurrency)
    async def run():
        return await asyncio.wait_for(coordinator.run(), 2)
    result = asyncio.run(run())
    assert state["active"] == 0 and state["peak"] == concurrency and state["extracts"] == 1
    assert [block["chapter_id"] for block in result["chapters"]] == [f"C{i}" for i in range(1, 7)]
    calls = model.calls
    assert asyncio.run(run()) == result and model.calls == calls
    for cache in tmp_path.glob("blocks-v4-segment-*.json"):
        cache.unlink()  # 模拟片段尚未提交完成，仍须校验内部章节检查点。
    for cache in tmp_path.glob("blocks-v4-chapter-*.json"):
        identifier = json.loads(cache.read_text(encoding="utf-8"))["chapter_id"]
        if identifier == "C1":
            cache.write_text('{}', encoding="utf-8")
        elif identifier == "C6":
            cache.unlink()
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert model.calls == calls  # 就绪的依赖缓存也应在补发新请求前完成校验。


@pytest.mark.parametrize("action", ["stop", "fail"])
def test_followup_stop_or_failure_saves_successes_and_resumes_missing_work(tmp_path, action):
    from app.errors import TaskPaused
    state = {"action": action, "started": [], "cancelled": [], "active": 0}
    class Model(FakeModel):
        def input_characters(self, operation, payload):
            if operation == "chapters" and (len(payload["chapters"]) > 1 or
                    any(chapter["chapter_id"] == "C1" and chapter.get("primary") for chapter in payload["chapters"])):
                return 1000000
            return super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                self.calls += 1
                value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": str(i), "core_question": "问题", "topics": ["主题"],
                         "start_anchor_id": anchor["id"]} for i, anchor in enumerate(payload["anchors"])], "video_summary": "导览"}
                validate(value)
                return value
            if operation == "overview":
                assert state["action"] is None
                return await super().generate(operation, payload, validate)
            identifier = payload["chapter"]["chapter_id"] if operation == "extract" else payload["chapters"][0]["chapter_id"]
            state["started"].append((operation, identifier))
            state["active"] += 1
            try:
                await asyncio.sleep(0)
                if state["action"] and operation == "chapters" and identifier != "C2":
                    if identifier == "C1":
                        if action == "fail":
                            raise AppError("invalid_model_output", "后续正文失败")
                        coordinator.stop.set()
                    await asyncio.Event().wait()
                return await super().generate(operation, payload, validate)
            except asyncio.CancelledError:
                state["cancelled"].append((operation, identifier))
                raise
            finally:
                state["active"] -= 1
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i,
                 "end_seconds": i + 0.5, "text": f"主题{i}原文"} for i in range(6)]
    coordinator, model, _ = make_generation(tmp_path, evidence, Model)
    async def run():
        return await asyncio.wait_for(coordinator.run(), 2)
    with pytest.raises(TaskPaused if action == "stop" else AppError):
        asyncio.run(run())
    assert state["active"] == 0 and state["cancelled"]
    assert ("chapters", "C6") not in state["started"]
    assert len(list(tmp_path.glob("blocks-v4-extract-*.json"))) == 1
    assert len(list(tmp_path.glob("blocks-v4-chapters-*.json"))) == 1
    assert not list(tmp_path.glob("blocks-v4-overview-*.json"))
    state["action"] = None
    coordinator.stop.clear()
    result = asyncio.run(run())
    assert len(result["chapters"]) == 6
    assert state["started"].count(("extract", "C1")) == state["started"].count(("chapters", "C2")) == 1


@pytest.mark.parametrize("shrinks", [True, False])
def test_dependent_extraction_reduces_again_or_fails_without_progress(tmp_path, shrinks):
    calls = []
    class Model(FakeModel):
        def input_characters(self, operation, payload):
            if operation == "chapters" and any(chapter.get("primary") or
                    sum(len(fact["text"]) for fact in chapter.get("facts", [])) > 40 for chapter in payload["chapters"]):
                return 1000000
            if operation == "extract" and len(payload["chapter"].get("facts", [])) > 1:
                return 1000000
            return super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            calls.append((operation, payload))
            if operation == "extract":
                self.calls += 1
                chapter = payload["chapter"]
                facts = ([{"text": item['text'] * 20} for item in chapter["primary"]]
                         if chapter.get("primary") else
                         [{**fact, "text": fact["text"][:10] if shrinks else fact["text"]} for fact in chapter["facts"]])
                value = {"facts": facts, "issues": []}
                validate(value)
                return value
            return await super().generate(operation, payload, validate)
    evidence = [{"id": f"s{i}", "kind": "speech", "start_seconds": i,
                 "end_seconds": i + 0.5, "text": f"主题{i}原文"} for i in range(2)]
    coordinator, _, _ = make_generation(tmp_path, evidence, Model)
    if shrinks:
        result = asyncio.run(coordinator.run())
        assert all(block["markdown"] for block in result["chapters"])
        assert all(set(fact) == {"text"} for operation, payload in calls if operation == "chapters" for fact in payload["chapters"][0]["facts"])
    else:
        with pytest.raises(AppError, match="章节信息仍超过预算"):
            asyncio.run(coordinator.run())
        assert not any(operation in {"chapters", "overview"} for operation, _ in calls)
    assert sum(operation == "extract" for operation, _ in calls) == 3


def test_followup_jobs_deduplicate_and_reuse_cached_dependencies(tmp_path):
    coordinator, model, _ = make_generation(tmp_path)
    initial = ("outline", {"anchors": coordinator.anchor_payload(), "evidence": coordinator.evidence})
    followup = ("overview", {"video_summaries": ["导览"]})
    received = []
    def completed(job, value):
        received.append(job[0])
        return [followup] if job[0] == "outline" else []
    values = asyncio.run(coordinator.pool([initial, initial], completed))
    assert values[0] == values[1] and model.calls == 2
    assert received.count("outline") == received.count("overview") == 2
    calls = model.calls
    received.clear()
    assert asyncio.run(coordinator.pool([initial, initial], completed)) == values
    assert model.calls == calls and received.count("outline") == received.count("overview") == 2


def test_empty_extraction_cannot_leave_over_budget_chapter_unscheduled(tmp_path):
    class Model(FakeModel):
        def input_characters(self, operation, payload):
            return 1000000 if operation == "chapters" else super().input_characters(operation, payload)
        async def generate(self, operation, payload, validate):
            if operation == "extract":
                value = {"facts": [], "issues": []}
                validate(value)
                return value
            return await super().generate(operation, payload, validate)
    coordinator, _, _ = make_generation(tmp_path, model_type=Model)
    with pytest.raises(AppError, match="章节信息仍超过预算"):
        asyncio.run(coordinator.run())
    assert not list(tmp_path.glob("blocks-v4-overview-*.json"))


@pytest.mark.parametrize("invalid", ["unknown_anchor", "duplicate_anchor", "reverse_anchor"])
def test_model_contract_rejects_invalid_boundaries_quotes_and_citations(tmp_path, invalid):
    coordinator, _, _ = make_generation(tmp_path, [
        {"id": "a", "kind": "speech", "start_seconds": 0, "end_seconds": 1, "text": "真实原文"},
        {"id": "b", "kind": "speech", "start_seconds": 2, "end_seconds": 3, "text": "第二主题"}])
    if invalid.endswith("anchor"):
        ids = {"unknown_anchor": ["E1", "unknown"], "duplicate_anchor": ["E1", "E1"], "reverse_anchor": ["E2", "E1"]}[invalid]
        value = {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "主题", "core_question": "问题", "topics": ["内容"], "start_anchor_id": i} for i in ids],
                 "video_summary": "导览"}
        with pytest.raises(ValueError):
            coordinator.validate("outline", {"anchors": coordinator.anchor_payload()}, value)


def test_source_issues_are_canonical_blocks_deduplicated_and_exported_without_time(tmp_path):
    from app.videos.notes import notes_text
    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            value = await super().generate(operation, payload, validate)
            if operation == "chapters":
                issue = {"kind": "source_issue", "quote": "原始证据", "reason": "名称无法确认。", "evidence_ids": ["E1"]}
                value["chapters"][0]["issues"].extend([issue, dict(issue)])
                validate(value)
            return value
    coordinator, _, _ = make_generation(tmp_path, model_type=Model)
    result = asyncio.run(coordinator.run())
    issues = result["source_issues"]
    assert len(issues) == 1
    assert not {"start_seconds", "end_seconds", "timestamp_basis"} & issues[0].keys()
    assert issues[0]["evidence_ids"] == ["source"]
    exported = notes_text("test.mp4", 20, result)
    assert "原文：原始证据" in exported and "窗口在慢启动" in exported
    footer = exported.split("存疑部分：")[1]
    assert "[" not in footer and "00:" not in footer


def test_oversized_prompt_blocks_generation_before_requests(tmp_path):
    coordinator, model, _ = make_generation(tmp_path)
    model.settings = replace(model.settings, summary_prompt="很长的偏好" * 10000)
    with pytest.raises(AppError) as error:
        asyncio.run(coordinator.run())
    assert error.value.code == "model_input_budget" and model.calls == 0
    assert not list(tmp_path.glob("blocks-v4-outline-*.json"))
    assert not list(tmp_path.glob("blocks-v4-outline_window-*.json"))
    assert not list(tmp_path.glob("blocks-v4-chapter-*.json"))


@pytest.mark.parametrize("version", [2, 3])
def test_obsolete_notes_are_not_returned_or_exported_and_records_are_preserved(client, app, video_file, version):
    from sqlalchemy import func
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    legacy = {"format_version": version, "overall_summary": "旧正文", "sections": [{"title": "旧章节", "points": []}],
              "blocks": [{"kind": "markdown", "markdown": "旧Markdown正文", "evidence_ids": []}]}
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.status, task.result = "succeeded", legacy
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert "result" not in task and task["notes_state"] == "none"
    for suffix in ("", "/notes"):
        response = client.get(f"/api/v1/analyses/{task_id}{suffix}")
        assert response.status_code == 409 and response.json()["error"]["code"] == "not_ready"
    with app.state.sessions() as session:
        assert session.get(Task, task_id).result == legacy
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.get(Video, video["id"]) is not None


@pytest.mark.parametrize("phase", ["outline", "overview"])
def test_stop_wakes_single_request_in_planning_or_overview(client, app, video_file, phase):
    import threading
    import time
    _, task_id, pipeline, _ = prepare(client, app, video_file)
    app.state.worker.register("video", Task, pipeline)
    started, closed, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Model(FakeModel):
        async def __aexit__(self, *exc):
            closed.set()
        async def generate(self, operation, payload, validate):
            if operation == phase:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = Model
    thread = threading.Thread(target=pipeline.run, args=(task_id,), daemon=True)
    thread.start()
    assert started.wait(5)
    tick = time.monotonic()
    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 202
    thread.join(2)
    assert not thread.is_alive() and time.monotonic() - tick < 2
    assert closed.is_set() and cancelled.is_set()
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "paused" and task["error"] is None
    assert pipeline.model_control is None


def test_stop_cancels_all_four_slow_chapter_requests_within_two_seconds(client, parallel_generation, monkeypatch):
    import threading
    import time
    _, task_id, pipeline, state = parallel_generation
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_section_concurrency=4): snapshot
    original_generate = pipeline.model_factory.generate
    started, cancelled, ready = [], [], threading.Event()
    async def generate(model, operation, payload, validate):
        if operation != "chapters":
            return await original_generate(model, operation, payload, validate)
        identifier = payload["chapters"][0]["chapter_id"]
        started.append(identifier)
        if len(started) == 4:
            ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(identifier)
            raise
    monkeypatch.setattr(pipeline.model_factory, "generate", generate)
    thread = threading.Thread(target=pipeline.run, args=(task_id,), daemon=True)
    thread.start()
    assert ready.wait(5)
    tick = time.monotonic()
    assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 202
    thread.join(2)
    assert not thread.is_alive() and time.monotonic() - tick < 2
    assert len(started) == 4 and set(cancelled) == set(started)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "paused"
    assert not state["finalized"] and state["closed"]


def test_stop_committed_during_failure_cleanup_wins(client, parallel_generation, monkeypatch):
    _, task_id, pipeline, state = parallel_generation
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), llm_section_concurrency=4): snapshot
    original_generate = pipeline.model_factory.generate
    ready = set()
    async def generate(model, operation, payload, validate):
        if operation != "chapters":
            return await original_generate(model, operation, payload, validate)
        identifier = payload["chapters"][0]["chapter_id"]
        ready.add(identifier)
        while len(ready) < 4:
            await asyncio.sleep(0)
        if identifier == "C1":
            raise AppError("invalid_model_output", "最终失败")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if identifier == "C2":
                assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 202
            raise
    monkeypatch.setattr(pipeline.model_factory, "generate", generate)
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "paused" and task["error"] is None and not state["finalized"]


def test_old_paused_task_is_rejected_without_upgrading_or_touching_cache(client, app, video_file):
    video, first_id, pipeline, recognizer = prepare(client, app, video_file)
    pipeline.run(first_id)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        first = session.get(Task, first_id)
        task = session.get(Task, task_id)
        config = dict(first.config)
        config["generation_flow"] = "old-flow"
        task.config, task.status = config, "paused"
        task.result = {"sections": [{"title": "旧结构", "points": []}]}
    folder = pipeline.get_settings().data_dir / "videos" / video["id"]
    legacy_cache = folder / "section-legacy.json"
    legacy_cache.write_text("旧生成缓存不得读取", encoding="utf-8")
    calls = []
    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            calls.append(operation)
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = Model
    assert client.post(f"/api/v1/tasks/{task_id}/resume").status_code == 409
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        assert task.config == config and task.status == "paused"
        task.status = "queued"
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "failed" and task["error"]["code"] == "generation_changed"
    assert calls == [] and recognizer.ocr_calls == 60
    with app.state.sessions() as session:
        assert session.get(Task, task_id).config == config
    assert legacy_cache.read_text(encoding="utf-8") == "旧生成缓存不得读取"


def test_identical_inputs_share_one_request_and_reuse_cache(tmp_path):
    coordinator, model, _ = make_generation(tmp_path)
    first = ("overview", {"video_summaries": ["主题"]})
    second = ("overview", {"video_summaries": ["不同主题"]})
    received = []
    jobs = [first, first, second, first]
    values = asyncio.run(coordinator.pool(jobs, lambda job, value: received.append(job)))
    assert model.calls == 2 and len(received) == 4
    assert values[0] == values[1] == values[3]
    assert len(list(tmp_path.glob("blocks-v4-overview-*.json"))) == 2
    model.calls = 0
    assert asyncio.run(coordinator.pool(jobs)) == values and model.calls == 0
