from unittest.mock import AsyncMock
from .fakes import no_delay
from .fakes import run_model
import errno
import json
import ssl
import subprocess
from contextlib import asynccontextmanager

import httpx
import pytest

from app.config import Settings
from app.diagnostics import Diagnostics
from app.errors import AppError
from app.videos.model import ModelClient


def log_text(directory, reference):
    return (directory / reference["log_file"].split("/")[-1]).read_text(encoding="utf-8")


@pytest.mark.parametrize("cause,expected", [
    (httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"), "SSL/TLS"),
    (ssl.SSLCertVerificationError("certificate verify failed"), "证书校验失败"),
    (httpx.ReadTimeout("timed out"), "超时"),
    (PermissionError(errno.EACCES, "permission denied"), "权限"),
    (OSError(errno.ENOSPC, "no space left"), "空间不足"),
    (FileNotFoundError("missing"), "不存在"),
    (json.JSONDecodeError("invalid JSON", "", 0), "数据格式"),
    (subprocess.TimeoutExpired(["ffmpeg"], 30, stderr=b"decoder stalled"), "超时"),
])
def test_report_summary_names_actual_cause(tmp_path, cause, expected):
    try:
        raise RuntimeError("outer operation failed") from cause
    except RuntimeError as exc:
        reference = Diagnostics(tmp_path).record("processing_failed", "处理失败。", exc)
    assert expected in reference["message"]
    text = log_text(tmp_path, reference)
    assert f"简要说明：{reference['message']}" in text
    assert type(cause).__name__ in text
    if isinstance(cause, subprocess.TimeoutExpired):
        assert "decoder stalled" in text


def test_grouped_error_keeps_cause_and_business_message(tmp_path):
    cause = ExceptionGroup("request failed", [AppError("model_not_downloaded", "请先下载模型文件。")])
    reference = Diagnostics(tmp_path).record("asr_failed", "语音识别失败。", cause)
    assert "请先下载模型文件" in reference["message"]
    assert "AppError" in log_text(tmp_path, reference)


def test_unknown_error_uses_fixed_summary_and_keeps_detail(tmp_path):
    reference = Diagnostics(tmp_path).record("unrecognized_code", "不可靠的原因推测。", RuntimeError("actual detail"))
    assert reference["message"] == "出现了未知错误，请查看错误报告。"
    assert "actual detail" in log_text(tmp_path, reference)
    assert "不可靠的原因推测" in log_text(tmp_path, reference)


@pytest.mark.parametrize("path,message", [
    ("/api/v1/documents/missing/extraction", "文档提取记录不存在或资料已被删除。"),
    ("/api/v1/documents/missing/content", "文档不存在或已被删除。"),
])
def test_document_missing_returns_specific_summary_and_matching_log(client, app, path, message):
    response = client.get(path)
    assert response.status_code == 404
    reference = response.json()["error"]
    assert reference["code"] == "document_missing"
    assert reference["message"] == message
    assert reference["log_saved"] is True
    text = log_text(app.state.pipeline.get_settings().log_dir, reference)
    assert f"简要说明：{message}" in text
    assert reference["error_id"] in text


@pytest.mark.parametrize("status,expected", [(401, "拒绝访问"), (403, "拒绝访问"), (429, "过于频繁"),
                                          (503, "暂时不可用"), (400, "拒绝了请求")])
def test_http_summary_and_full_response(tmp_path, status, expected):
    request = httpx.Request("POST", "https://test.invalid")
    response = httpx.Response(status, text="provider failure detail", request=request)
    cause = httpx.HTTPStatusError("request failed", request=request, response=response)
    reference = Diagnostics(tmp_path).record("model_failed", "模型失败。", cause)
    assert expected in reference["message"]
    assert "provider failure detail" in log_text(tmp_path, reference)
    assert f"HTTP 状态：{status}" in log_text(tmp_path, reference)


def test_user_input_cannot_spoof_ssl_category(client):
    response = client.put("/api/v1/settings/model", json={"base_url": "[SSL: CERTIFICATE_VERIFY_FAILED]"})
    assert response.status_code == 422
    assert "输入格式错误" in response.json()["error"]["message"]


def test_unsaved_token_and_quoted_credentials_are_redacted(tmp_path):
    secret = "unsaved-candidate-credential"
    request = httpx.Request("POST", "https://test.invalid", headers={"Authorization": f"Bearer {secret}"})
    response = httpx.Response(403, text=f"credential received: {secret}", request=request)
    cause = httpx.HTTPStatusError("forbidden", request=request, response=response)
    reference = Diagnostics(tmp_path).record("model_access_denied", "模型拒绝访问。", cause,
        extra='{"token": "two secret words", "authorization": "Basic dXNlcjpwYXNz"}')
    text = log_text(tmp_path, reference)
    assert secret not in text
    assert "two secret words" not in text
    assert "dXNlcjpwYXNz" not in text
    assert "HTTP 状态：403" in text


def test_log_directory_failure_returns_reference_without_hiding_original_error(tmp_path):
    directory = tmp_path / "not-a-directory"
    directory.write_text("occupied", encoding="utf-8")
    reference = Diagnostics(directory).record("failed", "原始操作失败。", RuntimeError("original"))
    assert reference["log_saved"] is False
    assert reference["message"] == "出现了未知错误，请查看错误报告。"
    assert "log_file" not in reference


def test_invalid_unicode_does_not_break_error_reporting(tmp_path):
    reference = Diagnostics(tmp_path).record("frontend_error", "bad unicode \ud800")
    assert reference["log_saved"] is True
    assert "bad unicode" in log_text(tmp_path, reference)


def test_early_api_error_has_matching_request_id(client, app):
    response = client.post("/api/v1/videos", headers={"content-length": "invalid"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_length"
    error = response.json()["error"]
    assert response.headers["x-request-id"] in log_text(app.state.pipeline.get_settings().log_dir, error)
    assert f"简要说明：{error['message']}" in log_text(app.state.pipeline.get_settings().log_dir, error)


def test_validation_message_uses_input_category(client, app):
    response = client.patch("/api/v1/videos/missing", json={"filename": ""})
    assert response.status_code == 422
    error = response.json()["error"]
    assert "输入格式错误" in error["message"]
    assert "API 地址" not in error["message"]


def test_browser_response_detail_is_logged_but_not_used_as_summary(client, app):
    response = client.post("/api/v1/diagnostics/client-errors", json={
        "code": "http_error", "message": "untrusted explanation", "stack": "SyntaxError at requestJSON",
        "request_url": "/api/v1/probe", "request_method": "GET", "http_status": 502,
        "response_body": '{"error": "proxy failure", "token": "private candidate"}',
    })
    assert response.status_code == 201
    reference = response.json()
    assert reference["message"] == "请求未能完成，请查看错误报告。"
    text = log_text(app.state.pipeline.get_settings().log_dir, reference)
    assert "proxy failure" in text
    assert "private candidate" not in text
    assert "502" in text


def test_connection_failure_is_not_reported_as_timeout(monkeypatch, tmp_path):
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    monkeypatch.setattr("app.analysis.llm.asyncio.sleep", no_delay)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=False))
    model.diagnostics = Diagnostics(tmp_path)
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code != "model_timeout"
    logs = [path.read_text(encoding="utf-8") for path in tmp_path.glob("*.log")]
    assert len(logs) == 2  # 两次待重试失败；最终失败由 API/任务边界记录。
    assert all("SSL/TLS" in text.split("【诊断信息】")[0] for text in logs)


@pytest.mark.parametrize("streaming", [True, False])
def test_provider_error_keeps_original_response(monkeypatch, tmp_path, streaming):
    body = {"error": {"code": "quota_exceeded", "message": "quota depleted"}}
    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield httpx.Response(200, text="data: " + json.dumps(body) + "\n")
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json=body)))
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=streaming))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    reference = Diagnostics(tmp_path).record(caught.value.code, caught.value.message, caught.value)
    text = log_text(tmp_path, reference)
    assert "quota_exceeded" in text
    assert "quota depleted" in text


def test_corrupt_download_state_keeps_parse_error(app):
    from app.recognition.local_models import ModelDownloads
    path = app.state.downloads.directory / "whisper-base.json"
    path.write_text("{broken", encoding="utf-8")
    ModelDownloads(app.state.pipeline.get_settings(), app.state.diagnostics)
    logs = list(app.state.pipeline.get_settings().log_dir.glob("*.log"))
    assert len(logs) == 1
    assert "JSONDecodeError" in logs[0].read_text(encoding="utf-8")
