import importlib
import json
import os
import sys
import threading
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.diagnostics import Diagnostics
from app.errors import AppError
from app.main import create_app
from app.settings.network_settings import NetworkSettingsInput, load_network_settings, effective_proxy
from app.video_download.process import downloader_args, network_error_hint, run_process
from app.video_download.tasks import VideoDownloads


def test_save_disable_and_restart(client, app):
    route = "/api/v1/settings/network"
    assert client.get(route).json() == {"enabled": False, "url": ""}
    value = {"enabled": True, "url": "socks5h://host.docker.internal:7890"}
    assert client.put(route, json=value).json() == value
    assert effective_proxy(app.state.video_downloads.get_settings()) == value["url"]
    value["enabled"] = False
    assert client.put(route, json=value).json() == value
    assert effective_proxy(app.state.video_downloads.get_settings()) == ""
    settings = app.state.pipeline.get_settings()
    path = settings.data_dir / "_private/network-settings.json"
    assert json.loads(path.read_text()) == value
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with TestClient(create_app(replace(settings, video_proxy_url=""), start_worker=False)) as restarted:
        assert restarted.get(route).json() == value
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("url", ["http://localhost:7890", "https://127.0.0.1:443", "socks5://[::1]:1080", "socks5h://proxy.local:1080"])
def test_supported_address(url):
    assert NetworkSettingsInput(enabled=True, url=url).url == url


@pytest.mark.parametrize("url", ["", "ftp://localhost:21", "localhost:7890", "http://", "http://user:pass@host:7890",
    "http://user@host:7890", "http://host:0", "http://host:65536", "http://host:abc", "socks5://:1080",
    "http://host:7890/path", "http://host:7890?x=y", "http://host:7890#x", "http://ho st:7890", "http://host:\n7890"])
def test_invalid_address_rejected(url):
    with pytest.raises(ValidationError):
        NetworkSettingsInput(enabled=True, url=url)


def test_save_failure_keeps_current_settings(client, app, monkeypatch):
    def fail(_):
        raise OSError("test disk failure")
    monkeypatch.setattr("app.settings.settings_service.save_network_settings", fail)
    response = client.put("/api/v1/settings/network", json={"enabled": True, "url": "http://localhost:7890"})
    assert response.status_code == 500
    assert client.get("/api/v1/settings/network").json() == {"enabled": False, "url": ""}
    assert effective_proxy(app.state.video_downloads.get_settings()) == ""


def test_private_route_rejects_external_origin(client):
    response = client.put("/api/v1/settings/network", headers={"Origin": "https://example.com"}, json={"enabled": False, "url": ""})
    assert response.status_code == 403


def test_no_saved_config_is_direct(tmp_path):
    assert effective_proxy(load_network_settings(Settings(data_dir=tmp_path))) == ""
    assert downloader_args()[downloader_args().index("--proxy") + 1] == ""


@pytest.mark.parametrize("url", ["https://www.bilibili.com/video/test", "https://www.youtube.com/watch?v=test", "https://www.tiktok.com/@test/video/1"])
def test_parse_uses_current_proxy_for_every_platform(url, client, monkeypatch):
    calls = []
    def parse(url, stop, proxy):
        calls.append(proxy)
        return {"url": url}
    monkeypatch.setattr("app.video_download.api.parse_video", parse)
    route = "/api/v1/video-downloads/parse"
    assert client.post(route, json={"url": url}).status_code == 200
    assert client.put("/api/v1/settings/network", json={"enabled": True, "url": "http://localhost:7890"}).status_code == 200
    assert client.post(route, json={"url": url}).status_code == 200
    assert calls == ["", "http://localhost:7890"]


def test_child_environment_isolated(monkeypatch):
    keys = ["HTTP_PROXY", "https_proxy", "All_Proxy", "NO_PROXY", "ftp_proxy"]
    for key in keys:
        monkeypatch.setenv(key, "test-value")
    output = run_process([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"], 10)
    child = json.loads(output)
    assert not any(key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"} for key in child)
    assert all(os.environ[key] == "test-value" for key in keys)


def test_task_keeps_start_configuration(tmp_path, monkeypatch):
    tasks = importlib.import_module("app.video_download.tasks")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def parse(url, stop, source, proxy):
        calls.append(("parse", proxy))
        entered.set()
        assert release.wait(5)
        source.write_text("{}")
        return {"url": url, "title": "test", "quality_options": []}
    def download(url, quality, directory, progress, stop, source, proxy):
        calls.append(("download", proxy))
        path = directory / "test.mp4"
        path.write_bytes(b"test")
        return path
    monkeypatch.setattr(tasks, "parse_video", parse)
    monkeypatch.setattr(tasks, "download_video", download)
    diagnostics = Diagnostics(tmp_path / "logs", lambda: "")
    from app.settings.current import CurrentSettings
    current = CurrentSettings(Settings(video_proxy_enabled=True, video_proxy_url="http://first:7890"))
    manager = VideoDownloads(tmp_path / "downloads", diagnostics, current.read)
    try:
        manager.start("https://example.com/video", 360)
        assert entered.wait(5)
        current.replace(replace(current.read(), video_proxy_url="socks5h://second:1080"))
        release.set()
        manager.thread.join(5)
        assert manager.current["status"] == "succeeded"
        manager.start("https://example.com/video", 360)
        manager.thread.join(5)
        assert calls == [("parse", "http://first:7890"), ("download", "http://first:7890"),
                         ("parse", "socks5h://second:1080"), ("download", "socks5h://second:1080")]
    finally:
        release.set()
        manager.close()
        diagnostics.close()


@pytest.mark.parametrize("host", ["www.bilibili.com", "b23.tv", "www.xiaohongshu.com", "xhslink.com", "www.douyin.com"])
@pytest.mark.parametrize("message", ["HTTP Error 403: Forbidden", "HTTP Error 412: Precondition Failed", "HTTP Error 429: Too Many Requests", "Connection refused"])
def test_domestic_routing_hint_only_with_proxy(host, message):
    error = AppError("video_download_failed", "ERROR: " + message)
    url = "https://" + host + "/video"
    enabled = network_error_hint(url, error, "http://localhost:7890")
    assert enabled.message.startswith(error.message)
    assert "分流规则" in enabled.message
    disabled = network_error_hint(url, error, "")
    assert "分流规则" not in disabled.message
    assert disabled.code == error.code and disabled.status == error.status


@pytest.mark.parametrize("message", ["HTTP Error 404: Not Found", "HTTP Error 410: Gone", "Video has been removed", "Video deleted", "Invalid URL", "Video does not exist"])
def test_missing_resource_has_no_hint(message):
    error = AppError("video_download_failed", "ERROR: " + message)
    assert network_error_hint("https://www.bilibili.com/video/x", error, "http://localhost:7890") is error


@pytest.mark.parametrize("message", ["Connection refused", "Temporary failure in name resolution", "Host not found", "SSL: CERTIFICATE_VERIFY_FAILED", "TLS handshake failed", "Read timed out"])
def test_network_hint_preserves_raw_error(message):
    error = AppError("video_download_failed", "ERROR: " + message)
    for proxy in ("", "http://localhost:7890"):
        result = network_error_hint("https://www.youtube.com/watch?v=x", error, proxy)
        assert result.message == error.message + "\n请检查「设置 → 网络代理」后重试。"


def test_parse_and_download_use_same_proxy_and_keep_cause(tmp_path, monkeypatch):
    from app.video_download import parse_video, download_video
    monkeypatch.setattr("app.video_download.metadata.read_parts", lambda *a: None)
    args_seen = []
    original = AppError("video_download_failed", "ERROR: HTTP Error 412: Precondition Failed")
    def fail(args, *a, **kw):
        args_seen.append(args)
        raise original
    for module in ("app.video_download.metadata", "app.video_download.download"):
        monkeypatch.setattr(importlib.import_module(module), "run_process", fail)
    for call in (lambda: parse_video("https://www.bilibili.com/video/x", proxy="https://proxy:443"),
                 lambda: download_video("https://www.bilibili.com/video/x", 360, tmp_path, proxy="https://proxy:443")):
        with pytest.raises(AppError) as caught:
            call()
        assert caught.value.__cause__ is original
        assert "分流规则" in caught.value.message
    assert all(args[args.index("--proxy") + 1] == "https://proxy:443" for args in args_seen)


@pytest.mark.parametrize("enabled,message,routing", [(True, "HTTP Error 412: Precondition Failed", True),
    (False, "HTTP Error 412: Precondition Failed", False), (True, "HTTP Error 404: Not Found", False),
    (True, "Connection refused", True), (False, "Connection refused", False)])
def test_api_error_and_report_keep_original(client, app, monkeypatch, enabled, message, routing):
    monkeypatch.setattr("app.video_download.metadata.read_parts", lambda *a: None)
    client.put("/api/v1/settings/network", json={"enabled": enabled, "url": "http://localhost:7890"})
    def fail(*a, **kw):
        raise AppError("video_download_failed", "ERROR: " + message)
    monkeypatch.setattr(importlib.import_module("app.video_download.metadata"), "run_process", fail)
    response = client.post("/api/v1/video-downloads/parse", json={"url": "https://www.bilibili.com/video/test"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "video_download_failed" and error["message"].startswith("ERROR: " + message)
    assert ("分流规则" in error["message"]) is routing
    report = (app.state.pipeline.get_settings().log_dir / error["log_file"].split("/")[-1]).read_text(encoding="utf-8")
    assert message in report and "AppError" in report and error["error_id"] in report
