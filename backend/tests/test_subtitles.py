import json
import pytest
from app.videos.pipeline import Pipeline
from app.db import Task, Video
from app.errors import AppError
from app.recognition.extraction import extraction_signature
from app.video_download.subtitles import (parse_subtitle, subtitle_candidates, youtube_source,
                                         content_digest, _download, fetch_subtitles)
from .conftest import upload, stored_task_config
from .fakes import FakeRecognizer, FakeModel


@pytest.mark.parametrize("extension,raw", [
    ("json3", '{"events":[{"tStartMs":100,"dDurationMs":1400,"segs":[{"utf8":"甲 &amp; 乙"}]}]}'),
    ("vtt", "WEBVTT\n\n00:00.100 --> 00:01.500 align:start\n<b>甲 &amp; 乙</b>\n"),
    ("srv3", '<timedtext><body><p t="100" d="1400"><s>甲 &amp; 乙</s></p></body></timedtext>'),
    ("srv2", '<timedtext><text t="100" d="1400">甲 &amp; 乙</text></timedtext>'),
    ("srv1", '<transcript><text start="0.1" dur="1.4">甲 &amp; 乙</text></transcript>'),
    ("ttml", '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="100ms" end="1.5s">甲 &amp; 乙</p></div></body></tt>'),
    ("srt", "1\n00:00:00,100 --> 00:00:01,500\n甲 &amp; 乙\n"),
])
def test_formats_have_same_text_and_seconds(extension, raw):
    assert parse_subtitle(raw, extension, 2) == [{"start_seconds": .1, "end_seconds": 1.5, "text": "甲 & 乙"}]


def test_rolling_updates_merge_only_overlapping_cues():
    raw = "WEBVTT\n\n00:00.000 --> 00:02.000\n甲\n\n00:01.000 --> 00:03.000\n甲乙\n\n00:04.000 --> 00:05.000\n甲乙\n"
    assert parse_subtitle(raw, "vtt", 10) == [
        {"start_seconds": 0, "end_seconds": 3, "text": "甲乙"},
        {"start_seconds": 4, "end_seconds": 5, "text": "甲乙"}]


def test_empty_invalid_outside_and_xml_entities():
    raw = {"events": [{"tStartMs": start, "dDurationMs": duration, "segs": [{"utf8": text}]}
                      for start, duration, text in [(-1, 10, "bad"), (0, 0, "bad"), (0, 1000, " "), (10000, 10, "bad")]]}
    assert parse_subtitle(json.dumps(raw), "json3", 2) == []
    with pytest.raises(ValueError):
        parse_subtitle('<!DOCTYPE a [<!ENTITY x "test">]><a/>', "srv3", 2)


def test_ttml_frames_and_line_breaks():
    raw = '<tt xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:frameRate="25"><body><p begin="00:00:01:05" dur="25f">甲<br/>乙</p></body></tt>'
    assert parse_subtitle(raw, "ttml", 5) == [{"start_seconds": 1.2, "end_seconds": 2.2, "text": "甲\n乙"}]


def test_format_tags_do_not_remove_literal_generic_types():
    raw = "WEBVTT\n\n00:00.000 --> 00:01.000\nList<T> 与 <c.green>重点</c>\n"
    assert parse_subtitle(raw, "vtt", 2)[0]["text"] == "List<T> 与 重点"


def test_candidate_selection_prefers_original_and_excludes_translations():
    def track(ext, url="https://www.youtube.com/api/timedtext?lang=en", **more):
        return {"ext": ext, "url": url, **more}
    info = {"language": "en", "subtitles": {"zh": [track("json3")], "en": [track("srv3"), track("json3")]},
            "automatic_captions": {"en-orig": [track("vtt")], "en": [track("json3", "https://www.youtube.com/?tlang=en")]}}
    candidates = subtitle_candidates(info, "zh")
    assert [(lang, auto) for lang, auto, formats in candidates] == [("en", False), ("en-orig", True)]
    assert [t["ext"] for t in candidates[0][2]] == ["json3", "srv3"]
    assert not subtitle_candidates({"subtitles": {"zh": [track("vtt", forced=True)]}}, "zh")


def test_format_failure_tries_srv3_and_hls_uses_downloader(monkeypatch, tmp_path):
    import yt_dlp
    formats = [{"ext": ext, "url": "https://test.invalid/sub", "protocol": "m3u8_native" if ext == "vtt" else "https"}
               for ext in ("srv3", "json3", "vtt")]
    calls = []
    class Downloader:
        def __init__(self, options): assert options["skip_download"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            assert download is False
            return {"subtitles": {"zh": formats}}
        def dl(self, filename, request, subtitle):
            calls.append((request["ext"], request["protocol"]))
            assert subtitle is True
            from pathlib import Path
            Path(filename).write_text('<timedtext><body><p t="0" d="1000">正文</p></body></timedtext>'
                                      if request["ext"] == "srv3" else "invalid", encoding="utf-8")
            return True, True
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    result = _download("https://www.youtube.com/watch?v=hh73MR7j_WM", "zh", 2, "", tmp_path)
    assert result["speech"][0]["text"] == "正文"
    assert calls == [("json3", "https"), ("vtt", "m3u8_native"), ("srv3", "https")]


def test_fetch_is_bounded_cancellable_and_only_subtitles(monkeypatch, tmp_path):
    from app.video_download import process
    import threading
    stopped = threading.Event()
    def run(args, timeout, stop):
        assert timeout == 90 and stop is stopped
        assert args[2:] == ["https://www.youtube.com/watch?v=hh73MR7j_WM", "zh", "2", "", args[-1]]
        return '{"speech":[],"reason":"unavailable"}'
    monkeypatch.setattr(process, "run_process", run)
    assert not fetch_subtitles(youtube_source("https://youtu.be/hh73MR7j_WM"), "zh", 2, tmp_path, stop=stopped)["speech"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("missing", [False, True])
def test_actual_hls_subtitle_downloads_only_playlist_and_vtt(monkeypatch, tmp_path, missing):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from yt_dlp import YoutubeDL
    media = tmp_path / "http"
    media.mkdir()
    (media / "sub.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nsub0.vtt\n" +
                                   ("#EXTINF:2,\nmissing.vtt\n" if missing else "") + "#EXT-X-ENDLIST\n", encoding="utf-8")
    (media / "sub0.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHLS 正文\n", encoding="utf-8")
    requests = []
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(self.path)
            return super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=media))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/sub.m3u8"
    monkeypatch.setattr(YoutubeDL, "extract_info", lambda *args, **kwargs: {
        "automatic_captions": {"zh": [{"ext": "vtt", "url": url, "protocol": "m3u8_native"}]}})
    try:
        result = _download("https://www.youtube.com/watch?v=hh73MR7j_WM", "zh", 2, "", tmp_path)
        if missing:
            assert not result["speech"] and result["reason"] == "unreadable"
            return
        assert result["speech"], (result, requests)
        assert result["speech"][0]["text"] == "HLS 正文"
        assert result["source"]["automatic"] is True
        assert set(requests) == {"/sub.m3u8", "/sub0.vtt"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def subtitle_pipeline(client, app, video_file, monkeypatch, platform="youtube"):
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        row = session.get(Video, video["id"])
        origin = youtube_source("https://youtu.be/hh73MR7j_WM") if platform == "youtube" else {
            "platform": "bilibili", "video_id": "BV1UhpK6RErH", "page": 1, "cid": 123,
            "url": "https://www.bilibili.com/video/BV1UhpK6RErH?p=1"}
        row.media_info = {**row.media_info, "source": origin}
        row.audio_codec = "aac"
    recognizer = FakeRecognizer(None)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    speech = [{"start_seconds": 0, "end_seconds": 1.5, "text": "TCP 慢启动：拥塞窗口增长"}]
    result = {"speech": speech, "source": {"kind": platform + "_subtitle", "language": "zh", "automatic": False,
                                           "digest": content_digest(speech)}}
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args, **kwargs: result)
    return video, pipeline, recognizer, result


@pytest.mark.parametrize("platform", ["youtube", "bilibili"])
def test_subtitle_end_to_end_speech_anchors_dedup_exports_and_resume(client, app, video_file, monkeypatch, platform):
    video, pipeline, recognizer, result = subtitle_pipeline(client, app, video_file, monkeypatch, platform)
    class DeduplicatedModel(FakeModel):
        async def generate(self, operation, payload, validate):
            if operation == "outline":
                sent = json.loads(self.messages(operation, payload)[-1]["content"])
                assert len(sent["evidence"]) == 1 and sent["evidence"][0]["text"] == result["speech"][0]["text"]
            return await super().generate(operation, payload, validate)
    pipeline.model_factory = DeduplicatedModel
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda *args: pytest.fail("字幕不得提取音频"))
    recognizer.transcribe = lambda *args: pytest.fail("字幕不得运行 ASR")
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(task_id)
    status = client.get(f"/api/v1/tasks/{task_id}").json()
    assert status["status"] == "succeeded", status["error"]
    assert status["progress"]["timing_labels"]["transcribing"] == "读取平台字幕"
    notes = client.get(f"/api/v1/analyses/{task_id}").json()
    assert notes["chapters"][0]["timestamp_basis"] == "speech"
    assert notes["recognition"]["transcript_source"] == result["source"]
    transcript = client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={task_id}&format=json').json()
    assert transcript["speech"][0]["kind"] == "speech" and transcript["transcript_source"] == result["source"]
    assert "平台字幕" in client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={task_id}').text
    config = stored_task_config(app, task_id)
    assert extraction_signature(config) != extraction_signature({k: v for k, v in config.items() if k != "transcript_source"})
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args: pytest.fail("恢复不重新选字幕"))
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "interrupted"
    assert client.post(f"/api/v1/tasks/{task_id}/retry").status_code == 202
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert recognizer.ocr_calls == 60


def test_unavailable_subtitle_falls_back_asr_once_and_keeps_ocr_cache(client, app, video_file, monkeypatch):
    video, pipeline, recognizer, _ = subtitle_pipeline(client, app, video_file, monkeypatch)
    calls = []
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args: calls.append(1) or {"speech": [], "reason": "unavailable"})
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda a, b, media_info=None: b.write_bytes(b"audio"))
    endpoint = f'/api/v1/videos/{video["id"]}/analyses'
    task_id = client.post(endpoint).json()["task_id"]
    pipeline.run(task_id)
    assert stored_task_config(app, task_id)["transcript_source"]["kind"] == "asr"
    assert client.get(f"/api/v1/tasks/{task_id}").json()["warnings"]
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "interrupted"
    client.post(f"/api/v1/tasks/{task_id}/retry")
    pipeline.run(task_id)
    assert calls == [1] and recognizer.asr_calls == 1 and recognizer.ocr_calls == 60


def test_cancel_during_fetch_does_not_fall_back(client, app, video_file, monkeypatch):
    video, pipeline, recognizer, _ = subtitle_pipeline(client, app, video_file, monkeypatch)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    def stopped(*args):
        with app.state.sessions.begin() as session:
            session.get(Task, task_id).status = "stopping"
        pipeline.cancel_model(task_id)
        assert args[-1].is_set()
        raise AppError("download_interrupted", "stopped")
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", stopped)
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "paused"
    assert recognizer.asr_calls == 0 and recognizer.ocr_calls == 0
    assert "transcript_source" not in stored_task_config(app, task_id)


def test_new_subtitles_keep_old_asr_evidence_and_share_ocr(client, app, video_file, monkeypatch):
    video, pipeline, recognizer, subtitle = subtitle_pipeline(client, app, video_file, monkeypatch)
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda a, b, media_info=None: b.write_bytes(b"audio"))
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args: {"speech": [], "reason": "unavailable"})
    endpoint = f'/api/v1/videos/{video["id"]}/analyses'
    first = client.post(endpoint).json()["task_id"]
    pipeline.run(first)
    old = client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={first}&format=json').json()
    monkeypatch.setattr("app.videos.pipeline.fetch_subtitles", lambda *args: subtitle)
    second = client.post(endpoint).json()["task_id"]
    pipeline.run(second)
    new = client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={second}&format=json').json()
    assert old["speech"][0]["text"] != new["speech"][0]["text"]
    assert client.get(f'/api/v1/videos/{video["id"]}/transcript?task_id={first}&format=json').json() == old
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60
    assert old["frames"][0]["image_name"] == new["frames"][0]["image_name"]
    assert old["frames"][0]["id"] != new["frames"][0]["id"]


def test_lost_selected_subtitles_fail_without_silently_switching_source(client, app, video_file, monkeypatch):
    from app.video_download.subtitles import subtitle_cache
    video, pipeline, recognizer, result = subtitle_pipeline(client, app, video_file, monkeypatch)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    pipeline.run(task_id)
    subtitle_cache(pipeline.get_settings().data_dir / "videos" / video["id"], result["source"]).unlink()
    with app.state.sessions.begin() as session:
        session.get(Task, task_id).status = "interrupted"
    client.post(f"/api/v1/tasks/{task_id}/retry")
    pipeline.run(task_id)
    assert client.get(f"/api/v1/tasks/{task_id}").json()["error"]["code"] == "file_missing"
    assert recognizer.asr_calls == 0
