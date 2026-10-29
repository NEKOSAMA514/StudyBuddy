import io
import math
import subprocess
import sys
import threading
import time
import wave
import pytest
from app.errors import AppError
from app.util.file_ingest import save_stream
from app.videos.video_reader import probe, extract_audio, extract_frame, prepare_playback, source_path
from app.util.media_process import command
from app.videos.playback import PlaybackCopies
from .conftest import upload
from .support import completed_download


@pytest.mark.parametrize("kind", ["audio", "frame"])
@pytest.mark.parametrize("outcome", ["cancel", "error", "success"])
@pytest.mark.parametrize("existing", [False, True])
def test_extraction_only_promotes_complete_output_and_preserves_cache(tmp_path, monkeypatch, kind, outcome, existing):
    from pathlib import Path
    from PIL import Image
    cancel = threading.Event()
    target = tmp_path / ("audio.wav" if kind == "audio" else "frame.jpg")
    def valid(path):
        if kind == "audio":
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * 160)
        else:
            Image.new("RGB", (16, 16), "white").save(path, format="JPEG")
    if existing:
        valid(target)
    previous = target.read_bytes() if existing else None
    produced = []
    def execute(args, timeout=300, cancel=None):
        temporary = Path(args[-1])
        produced.append(temporary)
        assert temporary != target and temporary.suffix == target.suffix
        temporary.write_bytes(b"partial-output")
        assert (target.read_bytes() if target.exists() else None) == previous
        if outcome == "cancel":
            cancel.set()
            raise AppError("media_cancelled", "cancelled")
        if outcome == "error":
            raise AppError("invalid_media", "decode failed")
        valid(temporary)
    monkeypatch.setattr("app.videos.video_reader.command", execute)
    media = {"audio": {"index": 0}, "video": {"index": 0}}
    def extract():
        if kind == "audio":
            extract_audio(tmp_path / "source.mp4", target, media, cancel)
        else:
            extract_frame(tmp_path / "source.mp4", target, 0, media, cancel)
    if outcome == "success":
        extract()
        assert target.is_file()
    else:
        with pytest.raises(AppError) as error:
            extract()
        assert error.value.code == ("media_cancelled" if outcome == "cancel" else "invalid_media")
        assert (target.read_bytes() if target.exists() else None) == previous
    assert produced and all(not item.exists() for item in produced)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True, capture_output=True)


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    folder = tmp_path_factory.mktemp("containers")
    cases = {"mp4": ("libx264", "aac"), "webm": ("libvpx-vp9", "libopus"),
             "mkv": ("libx264", "aac"), "mov": ("mpeg4", "pcm_s16le"),
             "avi": ("mpeg4", "mp3"), "flv": ("flv", "mp3"), "wmv": ("wmv2", "wmav2"),
             "mpg": ("mpeg2video", "mp2"), "ts": ("mpeg2video", "mp2"), "ogv": ("libtheora", "libvorbis")}
    for extension, (video, audio) in cases.items():
        ffmpeg("-f", "lavfi", "-i", "color=red:s=320x240:r=25:d=1", "-f", "lavfi", "-i", "sine=duration=1:sample_rate=44100",
               "-c:v", video, "-pix_fmt", "yuv420p", "-c:a", audio, "-threads", "1", folder / f"sample.{extension}")
    return folder


@pytest.mark.parametrize("extension", ["mp4", "webm", "mkv", "mov", "avi", "flv", "wmv", "mpg", "ts", "ogv"])
def test_real_containers_read_and_ingest(client, app, samples, tmp_path, extension):
    source = samples / f"sample.{extension}"
    info = probe(source)
    assert info["duration"] > 0 and info["audio_codec"]
    audio, frame = tmp_path / "audio.wav", tmp_path / "frame.jpg"
    extract_audio(source, audio)
    extract_frame(source, frame, 0.25)
    with wave.open(str(audio)) as stream:
        assert stream.getframerate() == 16000 and stream.getnframes() > 0
    assert frame.stat().st_size > 0
    video = upload(client, source)
    media = video["media_info"]
    stored = source_path(app.state.pipeline.get_settings().data_dir / "videos" / video["id"], media)
    assert stored.read_bytes() == source.read_bytes()
    assert media["container"] == info["media_info"]["container"]
    response = client.get(f'/api/v1/videos/{video["id"]}/content', headers={"Range": "bytes=0-31"})
    assert response.status_code == 206 and response.content == source.read_bytes()[:32]
    assert response.headers["content-type"] == media["mime_type"]
    identifier, _ = completed_download(app, source)
    imported = client.post(f"/api/v1/video-downloads/{identifier}/import").json()
    assert imported["id"] == video["id"] and imported["reused"]
    renamed = client.patch(f'/api/v1/videos/{video["id"]}', json={"filename": "没有扩展名", "pinned": True}).json()
    assert renamed["media_info"] == media
    assert client.get(f'/api/v1/videos/{video["id"]}/content').content == source.read_bytes()


def test_stream_limits_and_failure_cleanup(tmp_path):
    destination = tmp_path / "saved.bin"
    for content, limit, code in [(b"", 10, "empty_file"), (b"1234", 3, "upload_limit")]:
        with pytest.raises(AppError) as error:
            save_stream(io.BytesIO(content), destination, limit)
        assert error.value.code == code and not destination.exists()
    saved = save_stream(io.BytesIO(b"123"), destination, 3)
    assert saved["size_bytes"] == 3 and len(saved["fingerprint"]) == 64
    with pytest.raises(FileExistsError):
        save_stream(io.BytesIO(b"overwrite"), destination, 10)
    assert destination.read_bytes() == b"123"


def test_core_imports_without_web_database_or_recognizers():
    subprocess.run([sys.executable, "-c", "import sys; from app.util.file_ingest import save_stream; from app.videos.video_reader import probe, extract_audio, extract_frame, prepare_playback; assert not any(name in sys.modules for name in ('fastapi', 'sqlalchemy', 'app.recognition.media', 'app.videos.pipeline', 'app.analysis.llm', 'app.video_download'))"], check=True)


def test_selects_first_tracks_and_preserves_aspect_ratio(tmp_path):
    from app.videos.video_reader import direct_playback
    source, output = tmp_path / "tracks.mkv", tmp_path / "preview.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=720x576:r=25:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-map", "0:v", "-map", "1:a", "-map", "2:a",
           "-vf", "setsar=16/15", "-c:v", "mpeg4", "-c:a", "aac", "-disposition:a:0", "0", "-disposition:a:1", "default", source)
    info = probe(source)
    media = info["media_info"]
    assert media["video"]["index"] == 0 and media["audio"]["index"] == 1 and media["audio_tracks"] == 2
    assert not direct_playback(media)
    prepare_playback(source, output, media)
    preview = probe(output)["media_info"]
    assert preview["audio_tracks"] == 1
    assert preview["video"]["width"] <= 720 and preview["video"]["height"] <= 576
    assert math.isclose(preview["video"]["width"] / preview["video"]["height"], 4/3, abs_tol=0.01)


def test_actual_container_overrides_filename(client, app, video_file):
    response = client.post("/api/v1/videos", files={"file": ("wrong.webm", video_file.read_bytes())})
    video = response.json()
    assert response.status_code == 201 and video["media_info"]["container"] == "mp4"
    assert video["media_info"]["source_name"] == "original.mp4"
    same = upload(client, video_file)
    assert same["id"] == video["id"]


@pytest.mark.parametrize("codec,encoder", [("av1", "libaom-av1"), ("vp9", "libvpx-vp9")])
def test_complete_modern_mp4_offers_original_without_preparing_copy(client, app, tmp_path, monkeypatch, codec, encoder):
    source = tmp_path / f"{codec}.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=160x96:r=5:d=1", "-f", "lavfi", "-i", "sine=duration=1",
           "-c:v", encoder, "-cpu-used", "8", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", source)
    video = upload(client, source)
    assert video["media_info"]["video"]["codec_name"] == codec
    def forbidden(*args):
        pytest.fail("Reading playback metadata must not create a playback copy")
    monkeypatch.setattr(app.state.playback, "prepare", forbidden)
    state = client.get(f'/api/v1/videos/{video["id"]}/playback').json()
    assert state["direct"] and state["status"] == "unprepared"
    assert client.get(state["original_url"], headers={"Range": "bytes=0-31"}).content == source.read_bytes()[:32]


@pytest.mark.parametrize("change", [{"audio_tracks": 2}, {"video_tracks": 2}, {"audio": {"codec_name": "flac"}}])
def test_modern_mp4_keeps_track_and_audio_compatibility_checks(change):
    from app.videos.video_reader import direct_playback
    media = {"container": "mp4", "video": {"codec_name": "av1", "pix_fmt": "yuv420p"},
             "audio": {"codec_name": "aac"}, "video_tracks": 1, "audio_tracks": 1}
    assert not direct_playback({**media, **change})


def test_legacy_source_and_database_migration(tmp_path, video_file):
    import sqlite3
    from app.db import database, Video
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as database_file:
        database_file.execute("CREATE TABLE videos (id VARCHAR PRIMARY KEY, fingerprint VARCHAR UNIQUE, filename VARCHAR, size_bytes INTEGER, duration FLOAT, video_codec VARCHAR, audio_codec VARCHAR, created_at VARCHAR)")
        database_file.execute("INSERT INTO videos VALUES ('old','hash','旧笔记.mp4',12,2,'h264',NULL,'old-time')")
    engine, sessions = database(path)
    with sessions() as session:
        old = session.get(Video, "old")
        assert old.media_info is None and old.filename == "旧笔记.mp4" and not old.pinned
    folder = tmp_path / "old"
    folder.mkdir()
    original = folder / "original.mp4"
    original.write_bytes(video_file.read_bytes())
    assert source_path(folder) == original
    with pytest.raises(AppError):
        source_path(folder, {"source_name": "../legacy.sqlite3"})
    engine.dispose()


@pytest.mark.parametrize("extension,method", [("mkv", "remux"), ("mov", "transcode"), ("webm", "transcode"), ("avi", "transcode")])
def test_playback_modes(samples, tmp_path, extension, method):
    source = samples / f"sample.{extension}"
    before = source.read_bytes()
    output = tmp_path / "preview.mp4"
    assert prepare_playback(source, output) == method
    media = probe(output)["media_info"]
    assert media["container"] == "mp4" and media["video"]["codec_name"] == "h264"
    assert media["video"]["pix_fmt"] == "yuv420p" and media["audio"]["codec_name"] == "aac"
    assert (media["video"]["width"], media["video"]["height"]) == (320, 240)
    assert source.read_bytes() == before and not list(tmp_path.glob("*.tmp.mp4"))


def test_encode_max720_and_silent(tmp_path):
    source, output = tmp_path / "large.avi", tmp_path / "preview.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=1920x1080:d=0.4", "-c:v", "mpeg4", source)
    prepare_playback(source, output)
    info = probe(output)
    assert info["audio_codec"] is None
    assert (info["media_info"]["video"]["width"], info["media_info"]["video"]["height"]) == (1280, 720)


def wait_state(client, identifier, expected):
    for _ in range(100):
        result = client.get(f"/api/v1/videos/{identifier}/playback").json()
        if result["status"] in expected:
            return result
        time.sleep(0.05)
    pytest.fail(str(result))


def test_playback_api_cache_dedup_and_restart(client, app, samples):
    from fastapi.testclient import TestClient
    from app.main import create_app
    video = upload(client, samples / "sample.mkv")
    identifier = video["id"]
    assert client.get(f"/api/v1/videos/{identifier}/playback").json()["status"] == "unprepared"
    endpoint = f"/api/v1/videos/{identifier}/playback"
    for _ in range(3):
        assert client.post(endpoint).status_code == 202
    assert wait_state(client, identifier, {"ready", "failed"})["status"] == "ready"
    source = source_path(app.state.pipeline.get_settings().data_dir / "videos" / identifier, video["media_info"])
    output = app.state.playback.output(source)
    timestamp = output.stat().st_mtime_ns
    client.post(endpoint)
    assert output.stat().st_mtime_ns == timestamp
    assert client.get(endpoint + "/content", headers={"Range": "bytes=0-15"}).status_code == 206
    with TestClient(create_app(app.state.pipeline.get_settings(), start_worker=False)) as restarted:
        assert restarted.get(endpoint).json()["status"] == "ready"
    assert client.delete(f"/api/v1/videos/{identifier}").status_code == 200
    assert not output.exists()


def test_failed_copy_manual_retry_keeps_original(client, app, video_file, monkeypatch):
    import app.videos.playback as module
    video = upload(client, video_file)
    endpoint = f'/api/v1/videos/{video["id"]}/playback'
    original = module.prepare_playback
    calls = []
    def fail(*args):
        calls.append(1)
        raise AppError("invalid_media", "test failure")
    monkeypatch.setattr(module, "prepare_playback", fail)
    client.post(endpoint)
    failed = wait_state(client, video["id"], {"failed"})
    assert failed["error"]["error_id"]
    for _ in range(3):
        assert client.get(endpoint).json() == failed
    assert len(calls) == 1
    assert client.get(f'/api/v1/videos/{video["id"]}/content').content == video_file.read_bytes()
    monkeypatch.setattr(module, "prepare_playback", original)
    client.post(endpoint)
    assert wait_state(client, video["id"], {"ready", "failed"})["status"] == "ready"


@pytest.mark.parametrize("extension", ["webm", "mkv", "mov"])
def test_analysis_reads_original_and_resumes_without_repeating_recognition(client, app, samples, extension):
    from app.videos.pipeline import Pipeline
    from .fakes import FakeRecognizer, FakeModel
    video = upload(client, samples / f"sample.{extension}")
    identifier = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    class PausingRecognizer(FakeRecognizer):
        def ocr(self, image):
            result = super().ocr(image)
            if self.ocr_calls == 1:
                assert client.post(f"/api/v1/tasks/{identifier}/stop").status_code == 202
            return result
    recognizer = PausingRecognizer(None)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, FakeModel)
    pipeline.run(identifier)
    assert client.get(f"/api/v1/tasks/{identifier}").json()["status"] == "paused"
    client.post(f"/api/v1/tasks/{identifier}/resume")
    pipeline.run(identifier)
    state = client.get(f"/api/v1/tasks/{identifier}").json()
    assert state["status"] == "succeeded", state["error"]
    assert recognizer.asr_calls == 1 and recognizer.ocr_calls == 60
    folder = app.state.pipeline.get_settings().data_dir / "videos" / video["id"]
    assert source_path(folder, video["media_info"]).read_bytes() == (samples / f"sample.{extension}").read_bytes()
    assert not list(folder.glob("playback-*"))


def test_delete_cancels_preparing_copy(client, app, video_file, monkeypatch):
    import app.videos.playback as module
    entered = threading.Event()
    def blocked(source, output, media, cancel):
        entered.set()
        assert cancel.wait(5)
        raise AppError("media_cancelled", "cancelled")
    monkeypatch.setattr(module, "prepare_playback", blocked)
    video = upload(client, video_file)
    client.post(f'/api/v1/videos/{video["id"]}/playback')
    assert entered.wait(2)
    assert client.delete(f'/api/v1/videos/{video["id"]}').status_code == 200
    assert video["id"] not in app.state.playback.jobs
    assert not (app.state.pipeline.get_settings().data_dir / "videos" / video["id"]).exists()


def test_queue_merge_cancel_and_close(tmp_path, video_file, monkeypatch):
    import app.videos.playback as module
    from app.diagnostics import Diagnostics
    entered, calls = threading.Event(), []
    def blocked(source, output, media, cancel):
        calls.append(source)
        entered.set()
        assert cancel.wait(5)
        raise AppError("media_cancelled", "cancelled")
    monkeypatch.setattr(module, "prepare_playback", blocked)
    diagnostics = Diagnostics(tmp_path / "log", lambda: "")
    copies = PlaybackCopies(diagnostics)
    media = probe(video_file)["media_info"]
    copies.prepare("one", video_file, media)
    assert entered.wait(2)
    copies.prepare("one", video_file, media)
    copies.prepare("two", video_file, media)
    copies.cancel("two")
    assert len(calls) == 1
    copies.close()
    assert copies.jobs == {}
    diagnostics.close()


def test_cancel_kills_process_and_cleans_temporary(tmp_path, video_file):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    start = time.monotonic()
    with pytest.raises(AppError) as error:
        command([sys.executable, "-c", "import time; time.sleep(30)"], cancel=cancel)
    assert error.value.code == "media_cancelled" and time.monotonic() - start < 3
    output = tmp_path / "preview.mp4"
    with pytest.raises(AppError):
        prepare_playback(video_file, output, cancel=cancel)
    assert not output.exists() and not list(tmp_path.glob("*.tmp.mp4"))


@pytest.mark.parametrize("extension", ["mkv", "mp4"])
def test_nonzero_start_audio_delay_and_vfr_align(tmp_path, extension):
    from PIL import Image
    import numpy as np
    source = tmp_path / f"offset.{extension}"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=2", "-f", "lavfi", "-i", "color=blue:s=160x120:r=25:d=2",
           "-itsoffset", "1", "-f", "lavfi", "-i", "sine=duration=2:sample_rate=16000",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,select='if(lt(t,2),not(mod(n,2)),1)'[v]",
           "-map", "[v]", "-map", "2:a", "-fps_mode", "vfr", "-c:v", "libx264", "-c:a", "aac" if extension == "mp4" else "pcm_s16le",
           "-output_ts_offset", "5", source)
    info = probe(source)
    assert math.isclose(info["duration"], 4, abs_tol=0.15)
    media = info["media_info"]
    assert media["start_time"] >= 5
    audio, frame, output = tmp_path / "audio.wav", tmp_path / "frame.png", tmp_path / "preview.mp4"
    extract_audio(source, audio)
    with wave.open(str(audio)) as file:
        data = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16)
    assert np.max(np.abs(data[:12000])) == 0
    assert np.max(np.abs(data[20000:30000])) > 100
    extract_frame(source, frame, 2.5)
    assert Image.open(frame).getpixel((80, 60))[2] > 200
    prepare_playback(source, output)
    extract_frame(output, frame, 2.5)
    assert Image.open(frame).getpixel((80, 60))[2] > 200
    preview = probe(output)
    assert math.isclose(preview["duration"], 4, abs_tol=0.15)
    extracted = tmp_path / "preview-audio.wav"
    extract_audio(output, extracted)
    with wave.open(str(extracted)) as file:
        data = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16)
    assert np.max(np.abs(data[:12000])) < 5 and np.max(np.abs(data[20000:30000])) > 100
