from app.config import Settings
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from app.diagnostics import Diagnostics
from app.errors import AppError
from app.video_download import parse_video, download_video
from app.video_download.download import verify_video
from app.video_download.process import downloader_args, run_process
from app.video_download.urls import video_url
from app.video_download.tasks import VideoDownloads

metadata_module = importlib.import_module("app.video_download.metadata")
download_module = importlib.import_module("app.video_download.download")
tasks_module = importlib.import_module("app.video_download.tasks")


def info(heights=(360, 480, 720, 1080)):
    return {"id": "test", "title": "测试视频", "duration": 15, "thumbnail": "https://example.com/cover.jpg",
            "formats": [{"format_id": str(height), "url": f"https://example.com/{height}.mp4",
                         "height": height, "vcodec": "av1", "acodec": "aac", "ext": "mp4"} for height in heights]}


@pytest.mark.parametrize("heights,default", [((144, 240, 360, 720, 1080), 360), ((144, 240, 480, 720), 480), ((480, 720), 480), ((144, 240), 240)])
def test_metadata_and_default_quality(monkeypatch, heights, default):
    calls = []
    def run(args, timeout, **kwargs):
        calls.append((args, timeout))
        return json.dumps(info(heights))
    monkeypatch.setattr(metadata_module, "run_process", run)
    result = parse_video("https://example.com/video")
    assert result["default_quality"] == default
    assert result["qualities"] == list(heights)
    assert result["title"] == "测试视频" and result["duration"] == 15
    assert "-J" in calls[0][0] and calls[0][1] == 45


def test_quality_sizes_follow_selected_streams_and_merge(monkeypatch):
    value = info((360, 720))
    value["formats"][0].update(filesize=10 * 1024 ** 2)
    value["formats"][1].update(ext="webm", filesize_approx=30 * 1024 ** 2)
    monkeypatch.setattr(metadata_module, "run_process", lambda *a, **k: json.dumps(value))
    monkeypatch.setattr(metadata_module.YoutubeDL, "urlopen", lambda *a, **k: pytest.fail("选画质不能再次请求网络"))
    result = parse_video("https://example.com/video")
    assert result["quality_options"] == [
        {"height": 360, "ext": "mp4", "filesize": 10 * 1024 ** 2, "approximate": False},
        {"height": 720, "ext": "webm", "filesize": 30 * 1024 ** 2, "approximate": True},
    ]
    # 初次 -J 结果可能携带另一档位的选流信息，不得混入当前档位。
    value["requested_formats"] = [value["formats"][1]]
    value["filesize"] = 99 * 1024 ** 2
    assert parse_video("https://example.com/video")["quality_options"][0]["filesize"] == 10 * 1024 ** 2
    value = info((360,))
    value["formats"][0].update(acodec="none", filesize=10 * 1024 ** 2)
    audio = {"format_id": "audio", "url": "https://example.com/audio.m4a", "ext": "m4a",
             "vcodec": "none", "acodec": "aac", "filesize": 2 * 1024 ** 2}
    value["formats"].append(audio)
    result = parse_video("https://example.com/video")["quality_options"][0]
    assert result == {"height": 360, "ext": "mp4", "filesize": 12 * 1024 ** 2, "approximate": True}
    audio.pop("filesize")
    assert parse_video("https://example.com/video")["quality_options"][0]["filesize"] is None


@pytest.mark.parametrize("complete_height,expected_size,approximate", [
    (360, 8, False),
    (240, 12, True),
    (720, 12, True),
    (None, 12, True),
])
def test_selected_quality_before_complete_file(monkeypatch, complete_height, expected_size, approximate):
    value = info((360,))
    value["formats"][0].update(acodec="none", filesize=10 * 1024 ** 2)
    value["formats"].append({"format_id": "audio", "url": "https://example.com/audio.m4a", "ext": "m4a",
                             "vcodec": "none", "acodec": "aac", "filesize": 2 * 1024 ** 2})
    if complete_height:
        complete = info((complete_height,))["formats"][0]
        complete.update(format_id="complete", filesize=8 * 1024 ** 2)
        value["formats"].append(complete)
    monkeypatch.setattr(metadata_module.YoutubeDL, "urlopen", lambda *a, **k: pytest.fail("选流不能重新请求网络"))
    option = metadata_module.quality_option(value, 360)
    assert option == {"height": 360, "ext": "mp4",
                      "filesize": expected_size * 1024 ** 2, "approximate": approximate}


def test_video_without_audio_still_downloadable(monkeypatch):
    value = info((360,))
    value["formats"][0].update(acodec="none", filesize=1024)
    monkeypatch.setattr(metadata_module.YoutubeDL, "urlopen", lambda *a, **k: pytest.fail("选流不能重新请求网络"))
    assert metadata_module.quality_option(value, 360) == {
        "height": 360, "ext": "mp4", "filesize": 1024, "approximate": False}


@pytest.mark.parametrize("complete_height,expected_size,approximate", [(720, 8, False), (1080, 12, True)])
def test_missing_360_uses_nearest_higher_source(monkeypatch, complete_height, expected_size, approximate):
    value = info((720,))
    value["formats"][0].update(acodec="none", filesize=10 * 1024 ** 2)
    value["formats"].append({"format_id": "audio", "url": "https://example.com/audio.m4a", "ext": "m4a",
                             "vcodec": "none", "acodec": "aac", "filesize": 2 * 1024 ** 2})
    complete = info((complete_height,))["formats"][0]
    complete.update(format_id="complete", filesize=8 * 1024 ** 2)
    value["formats"].append(complete)
    monkeypatch.setattr(metadata_module.YoutubeDL, "urlopen", lambda *a, **k: pytest.fail("选流不能重新请求网络"))
    assert metadata_module.quality_option(value, 360) == {
        "height": 360, "ext": "mp4", "filesize": expected_size * 1024 ** 2, "approximate": approximate}


def test_extractor_preference_does_not_override_selected_quality(monkeypatch):
    value = info((360, 720))
    value["formats"][0].update(preference=-10, filesize=1024)
    value["formats"][1].update(preference=10, filesize=2048)
    monkeypatch.setattr(metadata_module.YoutubeDL, "urlopen", lambda *a, **k: pytest.fail("选流不能重新请求网络"))
    assert metadata_module.quality_option(value, 360)["filesize"] == 1024


def test_metadata_snapshot_is_the_input_to_download(monkeypatch, tmp_path):
    value = info((144, 240, 360))
    monkeypatch.setattr(metadata_module, "run_process", lambda *a, **k: json.dumps(value))
    source = tmp_path / "source.info.json"
    metadata = parse_video("https://example.com/video", source_path=source)
    assert json.loads(source.read_text(encoding="utf-8")) == value
    path = tmp_path / "测试视频-240P.mp4"
    path.write_bytes(b"video")
    calls = []
    def run(args, timeout, on_line=None, stop=None):
        calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "video", "height": 240}], "format": {"duration": 2}})
        on_line("SF_FILE:" + json.dumps(str(path)))
    monkeypatch.setattr(download_module, "run_process", run)
    assert download_video(metadata["url"], 240, tmp_path, source_path=source) == path
    assert "--load-info-json" in calls[0] and str(source) in calls[0]
    assert metadata["url"] not in calls[0]


@pytest.mark.parametrize("snapshot", [False, True])
def test_bilibili_parse_and_download_keep_verified_user_agent(monkeypatch, tmp_path, snapshot):
    # 平台可能对部分随机浏览器版本返回 code=0、空流列表，最终报 No video formats found。
    agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    url = "https://www.bilibili.com/video/BV1UhpK6RErH?p=2"
    calls = []
    def read(args, timeout, **kwargs):
        calls.append(args)
        if "--user-agent" not in args or args[args.index("--user-agent") + 1] != agent:
            raise AppError("video_download_failed", "ERROR: [BiliBili] No video formats found!")
        return json.dumps(info((360, 720)))
    monkeypatch.setattr(metadata_module, "read_parts", lambda *a: None)
    monkeypatch.setattr(metadata_module, "run_process", read)
    source = tmp_path / "source.info.json" if snapshot else None
    result = parse_video(url, source_path=source, proxy="http://proxy.test:7890")
    assert result["qualities"] == [360, 720] and result["default_quality"] == 360
    assert calls[0][-1] == url
    path = tmp_path / "test-360P.mp4"
    path.write_bytes(b"video")
    def download(args, timeout, on_line=None, stop=None):
        calls.append(args)
        assert args[args.index("--user-agent") + 1] == agent
        on_line("SF_FILE:" + json.dumps(str(path)))
    monkeypatch.setattr(download_module, "run_process", download)
    monkeypatch.setattr(download_module, "verify_video", lambda *a: path)
    assert download_video(url, 360, tmp_path, source_path=source, proxy="http://proxy.test:7890") == path
    assert all(args[args.index("--proxy") + 1] == "http://proxy.test:7890" for args in calls)
    assert all("--cookies" not in args and "--cookies-from-browser" not in args for args in calls)


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=test", "https://example.com/video", "https://bilibili.com.example.com/video/test"])
def test_other_sites_keep_downloader_default_user_agent(monkeypatch, url):
    calls = []
    monkeypatch.setattr(metadata_module, "run_process", lambda args, *a, **k: calls.append(args) or json.dumps(info((360,))))
    assert parse_video(url)["default_quality"] == 360
    assert "--user-agent" not in calls[0]


@pytest.mark.parametrize("quality", [0, -1, 1.5, True])
def test_download_rejects_invalid_quality_before_request(monkeypatch, tmp_path, quality):
    monkeypatch.setattr(download_module, "run_process", lambda *a, **k: pytest.fail("无效画质不得请求下载"))
    with pytest.raises(AppError, match="有效的视频画质"):
        download_video("https://example.com/video", quality, tmp_path)


@pytest.mark.parametrize("url", ["file:///tmp/a", "--output=/tmp/a", "https://user:pass@example.com/a", "https://example.com/a https://example.com/b",
    "分享 https://b23.tv/one https://b23.tv/two", "分享 https://b23.tv.evil.invalid/one",
    "分享 https://b23.tv@evil.invalid/one", "BV1umnw6rELqX"])
def test_reject_non_video_link(url):
    with pytest.raises(AppError, match="单个有效"):
        video_url(url)


@pytest.mark.parametrize("value,expected", [
    ("【【魂游人物志】惊人的老人】 https://www.bilibili.com/video/BV1umnw6rELq/?share_source=copy_web&vd_source=tracking",
     "https://www.bilibili.com/video/BV1umnw6rELq/?share_source=copy_web&vd_source=tracking"),
    ("【在绀碧大厦击杀重锤，提前拿下八星铳！-哔哩哔哩】 https://b23.tv/pVpZMMe", "https://b23.tv/pVpZMMe"),
    ("【分享】\nhttps://b23.tv/pVpZMMe。", "https://b23.tv/pVpZMMe"),
    ("分享（https://www.bilibili.com/video/BV1umnw6rELq/?p=2&share_source=copy_web）",
     "https://www.bilibili.com/video/BV1umnw6rELq/?p=2&share_source=copy_web"),
    (" BV1umnw6rELq ", "https://www.bilibili.com/video/BV1umnw6rELq"),
    ("HTTPS://WWW.BILIBILI.COM/video/BV1umnw6rELq", "HTTPS://WWW.BILIBILI.COM/video/BV1umnw6rELq"),
    ("https://www.youtube.com/watch?v=test&feature=share", "https://www.youtube.com/watch?v=test&feature=share"),
])
def test_shared_bilibili_text_uses_video_link(value, expected):
    assert video_url(value) == expected


@pytest.mark.parametrize("value", [{"_type": "playlist", "entries": []}, {"is_live": True}, info(())])
def test_reject_playlist_live_or_missing_video(monkeypatch, value):
    monkeypatch.setattr(metadata_module, "run_process", lambda *a, **k: json.dumps(value))
    with pytest.raises(AppError):
        parse_video("https://example.com/video")


def test_download_uses_final_path_and_does_not_transcode(monkeypatch, tmp_path):
    path = tmp_path / "video.webm"
    path.write_bytes(b"video")
    updates, calls = [], []
    def run(args, timeout, on_line=None, stop=None):
        calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "video", "codec_name": "vp9", "height": 360}], "format": {"duration": "2"}})
        on_line('SF_PROGRESS:{"downloaded":50,"total":"NA","estimate":100,"format_id":"v"}')
        on_line("SF_STAGE:MoveFiles")
        on_line("SF_STAGE:Merger")
        on_line("SF_FILE:" + json.dumps(str(path)))
    monkeypatch.setattr(download_module, "run_process", run)
    assert download_video("https://example.com/video", 720, tmp_path, updates.append) == path
    assert updates[0]["progress"] == 50
    assert [item["stage"] for item in updates] == ["downloading", "merging", "checking"]
    args = calls[0]
    assert args[args.index("--format") + 1] == "bestvideo*+bestaudio/best/bestvideo"
    assert args[args.index("--format-sort") + 1] == "res:720,hasaud"
    assert "--format-sort-force" in args
    assert "--abort-on-unavailable-fragments" in args
    assert not any(flag in args for flag in ("--recode-video", "--remux-video", "--merge-output-format", "--write-subs", "--cookies"))


@pytest.mark.parametrize("streams,duration", [([], 2), ([{"codec_type": "audio"}], 2), ([{"codec_type": "video"}], 0), ([{"codec_type": "video"}], "nan")])
def test_invalid_output_is_not_success(monkeypatch, tmp_path, streams, duration):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"video")
    monkeypatch.setattr(download_module, "run_process", lambda *a, **k: json.dumps({"streams": streams, "format": {"duration": duration}}))
    with pytest.raises(AppError):
        verify_video(path, tmp_path)


def test_missing_or_outside_output(tmp_path):
    with pytest.raises(AppError):
        verify_video(tmp_path / "absent.mp4", tmp_path)
    with pytest.raises(AppError):
        verify_video(Path(__file__), tmp_path)


def test_actual_low_resolution_is_valid(monkeypatch, tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    monkeypatch.setattr(download_module, "run_process", lambda *a, **k: json.dumps({
        "streams": [{"codec_type": "video", "height": 240}], "format": {"duration": "2"}}))
    assert verify_video(path, tmp_path) == path


def wait_task(manager, identifier):
    for _ in range(100):
        value = manager.get(identifier)
        if value["status"] != "running":
            if manager.thread:
                manager.thread.join(timeout=1)
            return value
        time.sleep(0.02)
    pytest.fail("下载测试任务未结束")


def test_download_freezes_canonical_part_url(monkeypatch, tmp_path):
    canonical = "https://www.bilibili.com/video/BV1UhpK6RErH?p=2"
    def parse(url, stop, source_path, proxy):
        assert url == "https://b23.tv/test"
        source_path.write_text("{}", encoding="utf-8")
        return {"url": canonical, "title": "P2 第二讲", "selected_part": 2}
    def download(url, quality, directory, *args):
        assert url == canonical and quality == 360
        path = directory / "P2 第二讲-360P.mp4"
        path.write_bytes(b"test")
        return path
    monkeypatch.setattr(tasks_module, "parse_video", parse)
    monkeypatch.setattr(tasks_module, "download_video", download)
    manager = VideoDownloads(tmp_path / "downloads", Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    try:
        result = wait_task(manager, manager.start("https://b23.tv/test", 360)["id"])
        assert result["status"] == "succeeded" and result["url"] == canonical
        assert result["metadata"]["selected_part"] == 2
        assert Path(result["path"]).name.startswith("P2 第二讲")
    finally:
        manager.close()


def test_busy_failure_cleanup_and_new_failure(monkeypatch, tmp_path):
    ready, release = threading.Event(), threading.Event()
    def parse(*args):
        ready.set()
        release.wait(2)
        return {**info(), "url": args[0]}
    def fail(url, quality, directory, *args):
        (directory / "video.part").write_bytes(b"partial")
        raise AppError("video_download_failed", "test: SSL timeout from provider")
    monkeypatch.setattr(tasks_module, "parse_video", parse)
    monkeypatch.setattr(tasks_module, "download_video", fail)
    manager = VideoDownloads(tmp_path / "downloads", Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    try:
        first = manager.start("https://example.com/video", 720)
        assert ready.wait(1)
        with pytest.raises(AppError) as busy:
            manager.start("https://example.com/video", 720)
        assert busy.value.status == 409
        release.set()
        result = wait_task(manager, first["id"])
        assert result["status"] == "failed" and result["path"] is None
        assert result["error"]["message"] == "test: SSL timeout from provider"
        assert not list((manager.directory / first["id"]).glob("*.part"))
        second = manager.start("https://example.com/video", 720)
        result2 = wait_task(manager, second["id"])
        assert result2["error"]["error_id"] != result["error"]["error_id"]
    finally:
        release.set()
        manager.close()


def test_restart_marks_interruption_and_preserves_completed(tmp_path):
    directory = tmp_path / "downloads"
    for identifier, status in [("a" * 32, "running"), ("b" * 32, "succeeded")]:
        folder = directory / identifier
        folder.mkdir(parents=True)
        (folder / "video.webm").write_bytes(b"video")
        (folder / "task.json").write_text(json.dumps({"id": identifier, "status": status, "path": str(folder / "video.webm")}), encoding="utf-8")
    manager = VideoDownloads(directory, Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    assert manager.get("a" * 32)["status"] == "interrupted"
    assert not (directory / ("a" * 32) / "video.webm").exists()
    assert (directory / ("b" * 32) / "video.webm").exists()
    assert manager.get("b" * 32)["status"] == "succeeded"
    manager.close()


def test_completed_format_and_size_follow_file_without_rewriting_history(tmp_path):
    directory = tmp_path / "downloads"
    folder = directory / "测试视频-360P"
    folder.mkdir(parents=True)
    path = folder / "测试视频-360P.mp4"
    path.write_bytes(b"finished-video")
    value = {"id": "c" * 32, "status": "succeeded", "path": str(path), "quality": 360,
             "metadata": {"quality_options": [
                 {"height": 240, "ext": "webm", "filesize": 123, "approximate": True},
                 {"height": 360, "ext": "webm", "filesize": 999, "approximate": True}]}}
    state = folder / "task.json"
    state.write_text(json.dumps(value), encoding="utf-8")
    original = state.read_bytes()
    manager = VideoDownloads(directory, Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    for current in (None, value):
        manager.current = current
        options = manager.get(value["id"])["metadata"]["quality_options"]
        assert options[0] == value["metadata"]["quality_options"][0]
        assert options[1] == {"height": 360, "ext": "mp4", "filesize": path.stat().st_size, "approximate": False}
    assert state.read_bytes() == original
    assert value["metadata"]["quality_options"][1]["ext"] == "webm"
    manager.close()


def test_container_download_path_resolves_locally_without_rewriting(tmp_path):
    directory = tmp_path / "downloads"
    folder = directory / "历史视频-360P"
    folder.mkdir(parents=True)
    path = folder / "历史视频-360P.webm"
    path.write_bytes(b"finished-video")
    value = {"id": "d" * 32, "status": "succeeded", "path": "/data/downloads/历史视频-360P/历史视频-360P.webm",
             "relative_path": "downloads/历史视频-360P/历史视频-360P.webm"}
    state = folder / "task.json"
    state.write_text(json.dumps(value), encoding="utf-8")
    original = state.read_bytes()
    manager = VideoDownloads(directory, Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    assert manager.get(value["id"])["path"] == str(path.resolve())
    assert state.read_bytes() == original
    manager.close()


def test_reveal_only_opens_registered_finished_file(client, app, monkeypatch):
    manager = app.state.video_downloads
    identifier = "e" * 32
    folder = manager.directory / "历史视频-360P"
    folder.mkdir()
    path = folder / "历史视频-360P.webm"
    path.write_bytes(b"finished-video")
    value = {"id": identifier, "status": "succeeded", "path": "/data/downloads/历史视频-360P/历史视频-360P.webm",
             "relative_path": "downloads/历史视频-360P/历史视频-360P.webm"}
    state = folder / "task.json"
    state.write_text(json.dumps(value), encoding="utf-8")
    original = state.read_bytes()
    manager.locations[identifier] = folder
    opened = []
    monkeypatch.setattr("app.video_download.tasks.os.startfile", opened.append, raising=False)
    route = f"/api/v1/video-downloads/{identifier}/reveal"
    assert client.post(route, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert opened == []
    assert client.post(route, json={"path": "C:/Windows", "command": "ignored"}).json() == {"opened": True}
    assert opened == [str(folder)]
    assert state.read_bytes() == original
    path.unlink()
    assert client.get(f"/api/v1/video-downloads/{identifier}").json()["path"] is None
    assert client.post(route).status_code == 404
    assert len(opened) == 1
    value["status"] = "running"
    state.write_text(json.dumps(value), encoding="utf-8")
    assert client.post(route).status_code == 409
    assert client.post("/api/v1/video-downloads/invalid/reveal").status_code == 404
    assert len(opened) == 1


def test_named_folders_duplicate_download_and_restart(monkeypatch, tmp_path):
    def parse(url, stop, source_path, proxy):
        source_path.write_text("{}", encoding="utf-8")
        return {**info((720,)), "url": url}
    monkeypatch.setattr(tasks_module, "parse_video", parse)
    calls = []
    def download(url, quality, directory, *args):
        calls.append(quality)
        path = directory / "化学课堂-720P.webm"
        path.write_bytes(str(len(calls)).encode())
        return path
    monkeypatch.setattr(tasks_module, "download_video", download)
    manager = VideoDownloads(tmp_path / "downloads", Diagnostics(tmp_path / "log", lambda: ""), lambda: Settings())
    first = wait_task(manager, manager.start("https://example.com/video", 360)["id"])
    first_file = Path(first["path"])
    assert first_file.parent.name == first_file.stem == "化学课堂-720P"
    assert not (first_file.parent / "source.info.json").exists()
    assert first["relative_path"] == "downloads/化学课堂-720P/化学课堂-720P.webm"
    second = wait_task(manager, manager.start("https://example.com/video", 360)["id"])
    second_file = Path(second["path"])
    assert second_file.parent.name == second_file.stem == "化学课堂-720P (2)"
    assert first_file.read_bytes() == b"1" and second_file.read_bytes() == b"2"
    assert not (manager.directory / first["id"]).exists()
    manager.close()
    recovered = VideoDownloads(manager.directory, manager.diagnostics, lambda: Settings())
    assert recovered.get(first["id"])["path"] == first["path"]
    assert recovered.get(second["id"])["relative_path"] == second["relative_path"]
    recovered.close()


@pytest.mark.parametrize("timeout,cancel", [(0.1, False), (10, True)])
def test_process_timeout_and_cancellation(timeout, cancel):
    stop = threading.Event()
    if cancel:
        stop.set()
    start = time.monotonic()
    with pytest.raises(AppError) as error:
        run_process([sys.executable, "-c", "import time; time.sleep(20)"], timeout, stop=stop)
    assert error.value.code == ("download_interrupted" if cancel else "video_download_timeout")
    assert time.monotonic() - start < 5


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程树终止兜底")
@pytest.mark.parametrize("outcome", ["failed", "ineffective", "error", "timeout"])
def test_tree_termination_falls_back_when_command_does_not_stop_process(monkeypatch, outcome):
    from unittest.mock import Mock
    from app.video_download.process import terminate
    process = Mock(pid=123)
    process.poll.return_value = None
    process.kill.side_effect = lambda: setattr(process.poll, "return_value", -9)
    def command(*args, **kwargs):
        if outcome == "error":
            raise OSError("command unavailable")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args[0], 5)
        return subprocess.CompletedProcess(args[0], 1 if outcome == "failed" else 0)
    monkeypatch.setattr("app.video_download.process.subprocess.run", command)
    terminate(process)
    process.kill.assert_called_once()


def test_process_cleanup_error_does_not_replace_original_failure(monkeypatch):
    from unittest.mock import Mock
    process = Mock()
    process.stdout = Mock()
    process.stdout.__iter__ = Mock(return_value=iter(["progress\n"]))
    monkeypatch.setattr("app.video_download.process.subprocess.Popen", lambda *a, **k: process)
    def cleanup(_):
        raise PermissionError("simulated cleanup failure")
    monkeypatch.setattr("app.video_download.process.terminate", cleanup)
    def consume(_):
        raise AppError("download_interrupted", "原始中断原因")
    with pytest.raises(AppError, match="原始中断原因"):
        run_process(["test"], 10, on_line=consume)
    process.stdout.close.assert_called_once()


def test_api_download_does_not_register_or_analyse(monkeypatch, client, app, video_file):
    monkeypatch.setattr("app.video_download.api.parse_video", lambda *a, **k: {"url": "https://example.com/video", **info()})
    monkeypatch.setattr(tasks_module, "parse_video", lambda *a: {**info(), "url": a[0]})
    qualities = []
    def download(url, quality, directory, update, stop, source_path, proxy):
        qualities.append(quality)
        path = directory / "video.mp4"
        path.write_bytes(video_file.read_bytes())
        return path
    monkeypatch.setattr(tasks_module, "download_video", download)
    assert client.post("/api/v1/video-downloads/parse", json={"url": "https://example.com/video"}).status_code == 200
    started = client.post("/api/v1/video-downloads", json={"url": "https://example.com/video"})
    assert started.status_code == 202
    result = wait_task(app.state.video_downloads, started.json()["id"])
    assert result["status"] == "succeeded" and Path(result["path"]).is_file()
    assert client.get("/api/v1/video-downloads/" + result["id"]).json()["path"] == result["path"]
    assert client.get("/api/v1/videos").json() == []
    for quality in (144, 240):
        response = client.post("/api/v1/video-downloads", json={"url": "https://example.com/video", "quality": quality})
        assert response.status_code == 202
        assert wait_task(app.state.video_downloads, response.json()["id"])["status"] == "succeeded"
    assert qualities == [360, 144, 240]
    assert client.get("/api/v1/videos").json() == []
    for quality in (0, -1):
        assert client.post("/api/v1/video-downloads", json={"url": "https://example.com/video", "quality": quality}).status_code == 422
    assert client.post("/api/v1/video-downloads/parse", headers={"Origin": "https://foreign.invalid"}, json={"url": "https://example.com/video"}).status_code == 403


def test_real_ytdlp_dash_download_and_merge(monkeypatch, tmp_path):
    """真实 yt-dlp + FFmpeg，媒体为本测试生成，不访问第三方平台。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    media = tmp_path / "source"
    media.mkdir()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-f", "dash", "video.mpd"], check=True, cwd=media)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(media)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/video.mpd"
        snapshot = tmp_path / "source.info.json"
        result = parse_video(url, source_path=snapshot)
        assert result["default_quality"] == 360
        assert result["quality_options"][0]["ext"] == "mp4"
        updates = []
        path = download_video(url, 360, tmp_path / "result", updates.append, source_path=snapshot)
        assert path.is_file() and path.stat().st_size > 0
        assert path.name == result["title"] + "-video-360P.mp4"
        streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"]
        assert {stream["codec_type"] for stream in streams} >= {"video", "audio"}
        assert any(item["stage"] == "downloading" for item in updates)
        assert any(item["stage"] == "merging" for item in updates)

        # 同清晰度同时提供完整文件与分离流时，真实下载应直接保存完整文件。
        complete = media / "complete.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(complete)], check=True)
        value = json.loads(run_process([*downloader_args(), "-J", "--", url], 45))
        value["title"] = "化学课堂/第一讲:测试?*"
        value["formats"].append({"format_id": "complete360", "url": f"http://127.0.0.1:{server.server_port}/complete.mp4",
                                 "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "aac", "filesize": complete.stat().st_size})
        source = tmp_path / "mixed-formats.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        assert metadata_module.quality_option(value, 360)["filesize"] == complete.stat().st_size

        updates = []
        path = download_video(url, 360, tmp_path / "complete-result", updates.append, source_path=source)
        assert path.read_bytes() == complete.read_bytes()
        assert path.stem.startswith("化学课堂") and path.stem.endswith("-360P")
        assert not any(char in path.name for char in '<>:"/\\|?*')
        assert not any(item["stage"] == "merging" for item in updates)

        low = media / "complete240.webm"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=426x240:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libvpx-vp9", "-deadline", "realtime",
                        "-c:a", "libopus", str(low)], check=True)
        value["formats"].append({"format_id": "complete240", "url": f"http://127.0.0.1:{server.server_port}/complete240.webm",
                                 "ext": "webm", "height": 240, "vcodec": "vp9", "acodec": "opus", "filesize": low.stat().st_size})
        source.write_text(json.dumps(value), encoding="utf-8")
        option = metadata_module.quality_option(value, 240)
        path = download_video(url, 240, tmp_path / "low-result", source_path=source)
        assert option["ext"] == path.suffix[1:] == "webm"
        assert option["filesize"] == path.stat().st_size
        assert path.read_bytes() == low.read_bytes() and path.stem.endswith("-240P")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
