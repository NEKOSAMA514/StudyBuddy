from dataclasses import replace
import json
import subprocess

from PIL import Image
import pytest

from app.videos.pipeline import Pipeline
from app.config import Settings
from app.db import Task, Video
from app.recognition.extraction import (LEGACY_EXTRACT_VERSION, extraction_config, extraction_signature,
                                       frame_times, line_ocr_cache_path, ocr_cache_signature, speech_signature)
from app.videos.video_reader import UniformFrameReader, probe
from .conftest import upload, stored_task_config
from .fakes import FakeModel, FakeRecognizer


@pytest.mark.parametrize("duration", [2, 15, 326.287438, 675, 840.085333, 4620, 7200])
def test_saved_uniform_positions_are_duration_divided_by_60(duration):
    config = extraction_config(Settings(), {"extract_version": LEGACY_EXTRACT_VERSION, "frame_count": 60})
    positions = frame_times(config, duration)
    assert len(positions) == 60 and positions[0] == 0 and positions[-1] < duration
    assert positions == [i * (duration / 60) for i in range(60)]


def test_legacy_positions_signatures_and_speech_reuse():
    current = extraction_config(Settings())
    legacy = extraction_config(Settings(), {})
    uniform = {**legacy, "frame_count": 60}
    assert frame_times(legacy, 326.287438) == list(range(0, 326, 15))
    assert current["frame_interval"] == 1 and current["ocr_min_confidence"] == .90
    assert "frame_count" not in extraction_config(Settings(), legacy)
    assert extraction_signature(current) != extraction_signature(legacy)
    assert extraction_signature(current) != extraction_signature(uniform)
    assert extraction_signature(uniform) == extraction_signature({**uniform, "frame_interval": 120})
    assert speech_signature(current) == speech_signature(uniform) == extraction_signature(legacy)


def test_reader_uses_media_origin_and_one_decoder(tmp_path, monkeypatch):
    import av
    source = tmp_path / "offset.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x120:d=1",
                    "-f", "lavfi", "-i", "color=blue:s=160x120:d=1", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-output_ts_offset", "5", str(source)], check=True)
    media = probe(source)["media_info"]
    opened, original = [], av.open
    def recorded(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(av, "open", recorded)
    reader = UniformFrameReader(source, media)
    try:
        assert [seconds for seconds, _ in reader.samples(2, 1)] == [0, 1]
        for seconds, channel in [(0, 0), (1.5, 2)]:
            output = tmp_path / f"{seconds}.jpg"
            reader.save(output, seconds)
            with Image.open(output) as image:
                assert image.size == (160, 120) and image.getpixel((80, 60))[channel] > 200
    finally:
        reader.close()
    assert len(opened) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_new_pipeline_resumes_and_reuses_line_cache_and_serves_screenshots(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    class PausingRecognizer(FakeRecognizer):
        def ocr(self, frame):
            result = super().ocr(frame)
            if self.ocr_calls == 1:
                assert client.post(f"/api/v1/tasks/{task_id}/stop").status_code == 202
            return result
    recognizer = PausingRecognizer(None)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    folder = pipeline.get_settings().data_dir / "videos" / video["id"]
    config = stored_task_config(app, task_id)
    cache = line_ocr_cache_path(folder, ocr_cache_signature(config))
    pipeline.run(task_id)
    stopped = client.get(f"/api/v1/tasks/{task_id}").json()
    assert stopped["status"] == "paused" and stopped["stage"] == "ocr:1/2"
    assert recognizer.ocr_calls == 1 and not cache.exists()
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), frame_interval=120): snapshot
    assert client.post(f"/api/v1/tasks/{task_id}/resume").status_code == 202
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert recognizer.ocr_calls == 3 and cache.is_file()
    exported = client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={task_id}&format=json').json()
    assert [e["start_seconds"] for e in exported["frames"]] == [0, 1]
    notes = client.get(f"/api/v1/analyses/{task_id}").json()
    assert notes["recognition"]["frame_interval"] == 1
    assert notes["recognition"]["ocr_min_confidence"] == .90
    image = exported["frames"][-1]
    assert not (folder / image["image_name"]).exists()
    assert client.get(f'/api/v1/evidence/{image["id"]}/image').status_code == 200
    assert (folder / image["image_name"]).is_file()
    assert client.get(f"/api/v1/analyses/{task_id}/notes?format=md").status_code == 200
    pipeline.get_settings = lambda snapshot=replace(pipeline.get_settings(), frame_interval=15): snapshot
    monkeypatch.setattr(UniformFrameReader, "samples", lambda *args: pytest.fail("缓存命中不应重新扫图"))
    second = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(second)
    assert client.get(f"/api/v1/tasks/{second}").json()["status"] == "succeeded"
    assert recognizer.ocr_calls == 3


def test_new_pipeline_reuses_old_speech_cache(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    old = extraction_config(app.state.pipeline.get_settings(), {})
    folder = app.state.pipeline.get_settings().data_dir / "videos" / video["id"]
    (folder / f"speech-{extraction_signature(old)}.json").write_text(json.dumps([
        {"start_seconds": 0, "end_seconds": 1, "text": "原有语音缓存。"}]), encoding="utf-8")
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda *args: pytest.fail("不应重新提取音频"))
    recognizer = FakeRecognizer(None)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert recognizer.asr_calls == 0 and recognizer.ocr_calls == 2


def test_old_task_keeps_fixed_interval_even_when_retry_updates_generation(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    with app.state.sessions.begin() as session:
        task = session.get(Task, task_id)
        task.config = {**task.config, **extraction_config(app.state.pipeline.get_settings(), {})}
        task.status = "failed"
    assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 202
    assert "frame_count" not in stored_task_config(app, task_id)
    monkeypatch.setattr(UniformFrameReader, "save", lambda *args: pytest.fail("旧任务不应切换规则"))
    recognizer = FakeRecognizer(None)
    Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel).run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert recognizer.ocr_calls == 1
