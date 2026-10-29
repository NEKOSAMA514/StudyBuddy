import json
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.recognition.local_models import MODELS, model_path, ModelDownloads
from .support import fake_files


def test_catalog_contains_engines_and_bundled_models(client, app):
    result = client.get("/api/v1/settings/recognition")
    assert result.status_code == 200
    catalog = result.json()
    assert catalog["settings"]["asr_option"] == "paraformer"
    models = {item["id"]: item for item in catalog["models"]}
    assert {item["id"] for item in catalog["models"] if item["kind"] == "asr"} == {"paraformer", "whisper-small", "whisper-base"}
    assert models["rapidocr"]["ready"]
    assert {item["id"] for item in catalog["models"] if item["kind"] == "ocr"} == {"rapidocr"}
    assert not models["whisper-base"]["ready"]
    assert models["paraformer"]["description"] == "适用于中文（普通话）。中文优先，侧重速度和较低资源占用。"
    assert models["whisper-base"]["description"] == "适用于中文、英文等多种语言。速度与准确率折中，资源占用较低。"
    assert models["whisper-small"]["description"] == "适用于中文、英文等多种语言。更重视准确率，耗时更长、资源占用更高。"
    assert models["whisper-small"]["size"] == "约 486 MB"
    assert models["rapidocr"]["description"] == "适用于中文、英文，适合清晰 PPT 和视频截图。"
    assert models["rapidocr"]["size"] == "约 32 MB"
    assert result.headers["cache-control"] == "no-store"


def test_cannot_save_missing_weights_or_unlisted_model(client):
    result = client.put("/api/v1/settings/recognition", json={"asr_option": "whisper-small", "ocr_option": "rapidocr"})
    assert result.json()["error"]["code"] == "model_not_downloaded"
    assert client.post("/api/v1/settings/models/evil/download").status_code == 404


def test_removed_ocr_cannot_be_selected_or_managed(client, app):
    before = app.state.pipeline.get_settings()
    fake_files(before, "paraformer")
    response = client.put("/api/v1/settings/recognition", json={"asr_option": "paraformer", "ocr_option": "tesseract"})
    assert response.json()["error"]["code"] == "unknown_model"
    for action in ("download", "reveal"):
        assert client.post(f"/api/v1/settings/models/tesseract/{action}").status_code == 404
    assert app.state.pipeline.get_settings() is before


def test_removed_ocr_settings_rejected_before_creating_data(tmp_path):
    directory = tmp_path / "unused"
    with pytest.raises(ValueError):
        Settings(data_dir=directory, ocr_engine="tesseract").prepare()
    assert not directory.exists()


def test_recognition_choice_persists_and_llm_save_does_not_overwrite(client, app, tmp_path, monkeypatch):
    settings = app.state.pipeline.get_settings()
    client.get("/api/v1/settings/recognition")
    fake_files(settings, "whisper-small")
    response = client.put("/api/v1/settings/recognition", json={"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "auto"})
    assert response.status_code == 200
    assert app.state.pipeline.get_settings().asr_engine == "whisper"
    assert app.state.pipeline.get_settings().asr_model == "small"
    client.put("/api/v1/settings/model", json={"model": "test/model"})
    with TestClient(create_app(replace(settings, static_dir=tmp_path / "absent"), start_worker=False)) as restarted:
        saved = restarted.get("/api/v1/settings/recognition").json()["settings"]
        assert saved == {"asr_option": "whisper-small", "ocr_option": "rapidocr", "language": "auto"}


@pytest.mark.parametrize("identifier", ["whisper-cpp", "whisper-tiny", "funasr", "qwen3-asr", "fireredasr", "parakeet"])
def test_removed_models_cannot_be_selected_or_downloaded(client, app, identifier):
    before = app.state.pipeline.get_settings()
    response = client.put("/api/v1/settings/recognition", json={"asr_option": identifier, "ocr_option": "rapidocr", "language": "zh"})
    assert response.json()["error"]["code"] == "unknown_model"
    assert client.post(f"/api/v1/settings/models/{identifier}/download").status_code == 404
    assert app.state.pipeline.get_settings() is before


@pytest.mark.parametrize("engine, model", [("paraformer", "small"), ("whisper", "small"), ("whisper", "base")])
def test_supported_settings_prepare(tmp_path, engine, model):
    Settings(data_dir=tmp_path, asr_engine=engine, asr_model=model).prepare()


@pytest.mark.parametrize("engine, model", [("whisper", "tiny"), ("whisper_cpp", "base"), ("qwen3_asr", "qwen3-asr")])
def test_removed_settings_rejected_before_creating_data(tmp_path, engine, model):
    directory = tmp_path / "unused"
    with pytest.raises(ValueError):
        Settings(data_dir=directory, asr_engine=engine, asr_model=model).prepare()
    assert not directory.exists()


def test_failed_download_has_log_and_recoverable_status(app, monkeypatch):
    import httpx
    requests = []
    async def fail(self, *args, **kwargs):
        requests.append(args[0])
        raise httpx.ConnectError("simulated download unavailable")
    monkeypatch.setattr(httpx.AsyncClient, "get", fail)
    downloads = app.state.downloads
    downloads.run("whisper-base")
    status = downloads.states["whisper-base"]
    assert status["status"] == "failed"
    assert status["error"]["log_file"].endswith(status["error"]["error_id"] + ".log")
    assert status["message"] == status["error"]["message"]
    assert "网络连接失败" in status["message"]
    assert downloads.active is None
    assert requests == [f"{host}/api/models/Systran/faster-whisper-base/revision/main" for host in ("https://hf-mirror.com", "https://huggingface.co")]
    path = downloads.directory / "whisper-base.json"
    path.write_text(json.dumps({"status": "downloading"}), encoding="utf-8")
    restarted = ModelDownloads(app.state.pipeline.get_settings(), app.state.diagnostics)
    assert restarted.states["whisper-base"]["status"] == "interrupted"


def test_list_llm_options_does_not_return_token(client, monkeypatch):
    import httpx
    client.put("/api/v1/settings/model", json={"token": "private-test-token"})
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"data": [{"id": "test/model"}]}, request=httpx.Request("GET", args[0])))
    response = client.get("/api/v1/settings/model/options")
    assert response.json() == {"models": ["test/model"]}
    assert "private-test-token" not in response.text


@pytest.mark.parametrize("identifier", ["paraformer", "whisper-small", "whisper-base"])
def test_repository_download_verifies_hash_before_publishing(app, monkeypatch, identifier):
    import hashlib
    import httpx
    from contextlib import asynccontextmanager
    spec = {**MODELS[identifier], "files": ["config.json"]}
    monkeypatch.setitem(MODELS, identifier, spec)
    monkeypatch.setattr("app.recognition.local_models.verify_paraformer", lambda directory: None)
    payload = b'{"ok":true}'
    expected = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
    metadata = {"sha": "pinned-commit", "siblings": [{"rfilename": "config.json", "size": len(payload), "blobId": expected}]}
    requests = []
    async def get(self, url, **kwargs):
        requests.append(url)
        return httpx.Response(200, json=metadata, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        requests.append(args[1])
        yield httpx.Response(200, content=payload, request=httpx.Request("GET", args[1]))
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    downloads = app.state.downloads
    downloads.run(identifier)
    assert downloads.states[identifier]["status"] == "succeeded"
    path = model_path(app.state.pipeline.get_settings(), identifier) / "config.json"
    assert path.read_bytes() == payload
    revision = spec.get("revision", "main")
    assert requests == [f"https://hf-mirror.com/api/models/{spec['repo']}/revision/{revision}",
                        f"https://hf-mirror.com/{spec['repo']}/resolve/pinned-commit/config.json"]
    metadata["siblings"][0]["blobId"] = "wrong-checksum"
    downloads.run(identifier)
    assert downloads.states[identifier]["error"]["code"] == "model_checksum"
    assert path.read_bytes() == payload


@pytest.mark.parametrize("failure", ["metadata_connection", "metadata_http", "metadata_json", "metadata_files",
                                    "stream_connection", "stream_http", "stream_read", "checksum", "oversize", "paraformer"])
def test_mirror_failure_falls_back_to_official_with_checksums(app, monkeypatch, failure):
    import hashlib
    import httpx
    from contextlib import asynccontextmanager
    from app.errors import AppError
    identifier = "paraformer" if failure == "paraformer" else "whisper-base"
    spec = {**MODELS[identifier], "files": ["config.json"]}
    monkeypatch.setitem(MODELS, identifier, spec)
    payload = b'{"ok":true}'
    metadata = {"sha": "pinned-commit", "siblings": [{"rfilename": "config.json", "size": len(payload),
                "lfs": {"sha256": hashlib.sha256(payload).hexdigest()}}]}
    directory = model_path(app.state.pipeline.get_settings(), identifier)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_bytes(b"previous-file")
    requests, messages, verifications = [], [], []
    original_update = app.state.downloads.update
    def update(model, **values):
        messages.append(values.get("message", ""))
        original_update(model, **values)
    monkeypatch.setattr(app.state.downloads, "update", update)
    def verify(current):
        verifications.append(current)
        if len(verifications) == 1:
            raise AppError("asr_model_invalid", "模拟镜像文件未通过固定校验")
    monkeypatch.setattr("app.recognition.local_models.verify_paraformer", verify)
    async def get(self, url, **kwargs):
        requests.append(url)
        assert kwargs["params"] == {"blobs": "true"}
        if "hf-mirror.com" in url:
            if failure == "metadata_connection":
                raise httpx.ConnectError("mirror unavailable")
            if failure == "metadata_http":
                return httpx.Response(503, request=httpx.Request("GET", url))
            if failure == "metadata_json":
                return httpx.Response(200, content=b"{broken", request=httpx.Request("GET", url))
            if failure == "metadata_files":
                return httpx.Response(200, json={**metadata, "siblings": []}, request=httpx.Request("GET", url))
        elif failure != "paraformer":
            assert path.read_bytes() == b"previous-file"
        return httpx.Response(200, json=metadata, request=httpx.Request("GET", url))
    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        requests.append(url)
        assert "/resolve/pinned-commit/config.json" in url
        mirror = "hf-mirror.com" in url
        if mirror and failure == "stream_connection":
            raise httpx.ConnectError("mirror unavailable")
        body = b"x" * len(payload) if mirror and failure == "checksum" else payload + b"x" if mirror and failure == "oversize" else payload
        response = httpx.Response(503 if mirror and failure == "stream_http" else 200, content=body, request=httpx.Request(method, url))
        if mirror and failure == "stream_read":
            async def interrupted(chunk_size):
                yield payload[:2]
                raise httpx.ReadError("mirror stream interrupted")
            response.aiter_bytes = interrupted
        yield response
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    app.state.downloads.run(identifier)
    assert requests[0].startswith("https://hf-mirror.com/api/")
    assert any(url.startswith("https://huggingface.co/api/") for url in requests)
    assert requests[-1].startswith("https://huggingface.co/")
    assert path.read_bytes() == payload
    assert not path.with_suffix(".json.part").exists()
    assert app.state.downloads.states[identifier]["status"] == "succeeded"
    assert app.state.downloads.states[identifier]["error"] is None
    assert app.state.downloads.states[identifier]["downloaded_bytes"] == len(payload)
    assert "镜像下载未完成，正在切换到 Hugging Face…" in messages
    if failure == "paraformer":
        assert verifications == [directory, directory]


@pytest.mark.parametrize("operation", ["write", "replace"])
def test_download_disk_failure_does_not_switch_source(app, monkeypatch, operation):
    import hashlib
    import httpx
    from pathlib import Path
    from contextlib import asynccontextmanager
    identifier = "whisper-base"
    monkeypatch.setitem(MODELS, identifier, {**MODELS[identifier], "files": ["config.json"]})
    payload = b"test"
    metadata = {"sha": "pinned-commit", "siblings": [{"rfilename": "config.json", "size": len(payload),
                "lfs": {"sha256": hashlib.sha256(payload).hexdigest()}}]}
    requests = []
    async def get(self, url, **kwargs):
        requests.append(url)
        return httpx.Response(200, json=metadata, request=httpx.Request("GET", url))
    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        requests.append(url)
        yield httpx.Response(200, content=payload, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    directory = model_path(app.state.pipeline.get_settings(), identifier)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_bytes(b"previous-file")
    attribute = "open" if operation == "write" else "replace"
    original = getattr(Path, attribute)
    def fail(temporary, *args, **kwargs):
        if temporary == path.with_suffix(".json.part"):
            raise OSError("simulated disk failure")
        return original(temporary, *args, **kwargs)
    monkeypatch.setattr(Path, attribute, fail)
    app.state.downloads.run(identifier)
    assert len(requests) == 2 and all(url.startswith("https://hf-mirror.com/") for url in requests)
    assert path.read_bytes() == b"previous-file"
    assert app.state.downloads.states[identifier]["status"] == "failed"
