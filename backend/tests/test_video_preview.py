import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from app.config import Settings
from app.db import Task
from app.recognition.extraction import extraction_config, extraction_signature, speech_signature, speech_cache_path, ocr_cache_path, frame_times
from app.video_download.subtitles import content_digest, subtitle_cache
from app.videos.preview import analysis_preview
from app.videos.detailed import DetailedVideoGeneration
from app.videos.concise import ConciseVideoGeneration
from .conftest import upload
from .fakes import FakeModel


def caches(folder, video, config, speech=None):
    folder.mkdir(parents=True, exist_ok=True)
    signature = extraction_signature(config)
    if speech is not None:
        speech_cache_path(folder, speech_signature(config)).write_text(json.dumps(speech), "utf-8")
    evidence = [{**item, "id": f"{video.id}-{signature}-speech-{i}", "kind": "speech",
                 "image_name": None, "confidence": None} for i, item in enumerate(speech or [])]
    for i, seconds in enumerate(frame_times(config, video.duration)):
        item = {"text": f"第{i}帧的原文知识与适用条件。", "start_seconds": seconds, "end_seconds": seconds, "kind": "frame",
                "id": f"{video.id}-{signature}-frame-{i}", "image_name": f"frame-{signature}-{i:05d}.jpg", "confidence": 0.95}
        ocr_cache_path(folder, signature, i).write_text(json.dumps(item), "utf-8")
        evidence.append(item)
    return evidence


@pytest.mark.parametrize("style,limit,expected", [("concise", 25000, 1), ("detailed", 25000, 3), ("concise", 5500, None)])
def test_preview_matches_actual_generation_and_is_read_only(tmp_path, style, limit, expected):
    settings = Settings(note_style=style, llm_input_text_limit=limit, summary_prompt="解释适用条件")
    video = SimpleNamespace(id="preview", filename="课程.mp4", duration=3601, audio_codec="aac" if limit == 5500 else None)
    folder = tmp_path / "recognition"
    config = extraction_config(settings)
    speech = [{"text": "较长知识点解释与适用条件。\n" * 800, "start_seconds": 0, "end_seconds": 600}] if limit == 5500 else None
    evidence = caches(folder, video, config, speech)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    info = analysis_preview(video, folder, config, settings)
    assert info["character_count"] == sum(len(i["text"]) for i in evidence)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    async def run():
        async with FakeModel(settings) as model:
            args = ("task", model, settings, tmp_path / style, evidence, video.duration, asyncio.Event(), lambda: None, lambda _: None, {})
            (tmp_path / style).mkdir()
            gen = (ConciseVideoGeneration(*args, lambda _: None, "课程") if style == "concise"
                   else DetailedVideoGeneration(*args, video_title="课程"))
            return await gen.run()
    result = asyncio.run(run())
    assert info["estimated_segments"] == result["total_segments"]
    if expected is not None:
        assert info["estimated_segments"] == expected
    else:
        assert info["estimated_segments"] > 1


@pytest.mark.parametrize("missing", ["new", "speech", "frame", "corrupt", "changed-model"])
def test_incomplete_or_changed_recognition_is_unknown(tmp_path, missing):
    settings = Settings()
    config = extraction_config(settings)
    video = SimpleNamespace(id="preview", filename="课程.mp4", duration=10, audio_codec="aac")
    if missing != "new":
        caches(tmp_path, video, config, [{"text": "讲解", "start_seconds": 0, "end_seconds": 2}])
        if missing == "speech":
            speech_cache_path(tmp_path, speech_signature(config)).unlink()
        elif missing == "frame":
            ocr_cache_path(tmp_path, extraction_signature(config), 59).unlink()
        elif missing == "corrupt":
            ocr_cache_path(tmp_path, extraction_signature(config), 59).write_text("{", "utf-8")
        else:
            config = extraction_config(replace(settings, ocr_min_confidence=0.9))
    result = analysis_preview(video, tmp_path, config, settings)
    assert result["character_count"] is result["estimated_segments"] is None


def test_cached_subtitles_and_input_budget_are_used(tmp_path):
    settings = Settings(note_style="concise")
    video = SimpleNamespace(id="preview", filename="课程.mp4", duration=120, audio_codec="aac")
    config = extraction_config(settings)
    caches(tmp_path, video, config)
    speech = [{"text": "字幕讲解。" * 3000, "start_seconds": 0, "end_seconds": 100}]
    config["transcript_source"] = {"kind": "youtube_subtitle", "digest": content_digest(speech)}
    subtitle_cache(tmp_path, config["transcript_source"]).write_text(json.dumps(speech), "utf-8")
    large = analysis_preview(video, tmp_path, config, settings)
    small = analysis_preview(video, tmp_path, config, replace(settings, llm_input_text_limit=5500))
    assert small["character_count"] == large["character_count"]
    assert small["estimated_segments"] > large["estimated_segments"]
    subtitle_cache(tmp_path, config["transcript_source"]).write_text("[]", "utf-8")
    assert analysis_preview(video, tmp_path, config, settings)["character_count"] is None


def test_speech_uses_only_valid_timestamps(tmp_path):
    settings = Settings()
    config = extraction_config(settings)
    video = SimpleNamespace(id="preview", filename="课程.mp4", duration=10, audio_codec="aac")
    speech = [{"text": "正常讲解", "start_seconds": 0, "end_seconds": 20},
              {"text": "错误负值", "start_seconds": -1, "end_seconds": 2},
              {"text": "越界", "start_seconds": 10, "end_seconds": 12},
              {"text": "非数字时间", "start_seconds": float("nan"), "end_seconds": 2}]
    evidence = caches(tmp_path, video, config, speech)
    expected = len(speech[0]["text"]) + sum(len(item["text"]) for item in evidence if item["kind"] == "frame")
    assert analysis_preview(video, tmp_path, config, settings)["character_count"] == expected


def test_preview_api_is_read_only_validates_options_and_recomputes_profile(client, app, video_file):
    video = upload(client, video_file)
    url = f'/api/v1/videos/{video["id"]}/analysis-preview'
    assert client.get(url).json()["character_count"] is None
    from app.db import Video
    with app.state.sessions() as session:
        row = session.get(Video, video["id"])
    settings = app.state.pipeline.get_settings()
    caches(settings.data_dir / "videos" / row.id, row, extraction_config(settings))
    response = client.get(url)
    assert response.status_code == 200 and response.json()["estimated_segments"] == 1
    assert set(response.json()) == {"character_count", "estimated_segments", "reason"}
    assert client.get(url, params={"note_style": "invalid"}).status_code == 422
    assert client.get(url, params={"llm_profile_id": "default", "model": "other"}).status_code == 422
    assert client.get(url, params={"llm_profile_id": "missing"}).status_code == 404
    assert client.get('/api/v1/videos/missing/analysis-preview').status_code == 404
    profile = client.post('/api/v1/settings/model/profiles', json={"name": "tiny", "input_text_limit": 20}).json()["profiles"][-1]
    limited = client.get(url, params={"llm_profile_id": profile["id"], "note_style": "concise"}).json()
    assert limited["character_count"] == response.json()["character_count"]
    assert limited["estimated_segments"] is None and limited["reason"]
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_preview_api_reuses_executed_subtitle_source_not_new_queued_task(client, app, video_file):
    video = upload(client, video_file)
    from app.db import Video
    settings = app.state.pipeline.get_settings()
    config = extraction_config(settings)
    folder = settings.data_dir / "videos" / video["id"]
    with app.state.sessions() as session:
        row = session.get(Video, video["id"])
    evidence = caches(folder, row, config)
    speech = [{"text": "已有平台字幕", "start_seconds": 0, "end_seconds": 1}]
    source = {"kind": "youtube_subtitle", "digest": content_digest(speech)}
    subtitle_cache(folder, source).write_text(json.dumps(speech), "utf-8")
    with app.state.sessions.begin() as session:
        session.add(Task(id="old-subtitles", video_id=row.id, status="succeeded", created_at="2026-01-01",
                         config={**config, "execution_started": True, "transcript_source": source}))
        session.add(Task(id="new-queued", video_id=row.id, config=config, created_at="2026-01-02"))
    response = client.get(f'/api/v1/videos/{row.id}/analysis-preview')
    assert response.status_code == 200
    assert response.json()["character_count"] == len(speech[0]["text"]) + sum(len(i["text"]) for i in evidence)
