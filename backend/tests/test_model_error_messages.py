from .fakes import model_validator
from .fakes import run_model
import json
import socket
import ssl
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.diagnostics import Diagnostics
from app.errors import AppError
from app.analysis.llm import ModelClient as TransportClient
from app.videos.model import ModelClient
from app.videos.pipeline import Pipeline
from .conftest import upload
from .fakes import FakeRecognizer


UNKNOWN = "出现了未知错误，请查看错误报告。"
MESSAGES = [
    ('{"error":{"message":" SSL DNS timeout 原始错误 "},"message":"other"}', " SSL DNS timeout 原始错误 "),
    ('{"error":{"message":" "},"message":"top message"}', "top message"),
    ('{"error":"string error","message":"top message"}', "top message"),
    ('{"error":"string error"}', "string error"),
    ('{"error":{"code":"quota_exceeded"}}', '{"error":{"code":"quota_exceeded"}}'),
    ('{"error":{"message":42},"message":null}', '{"error":{"message":42},"message":null}'),
    ("<html>upstream SSL timeout</html>", "<html>upstream SSL timeout</html>"),
    ("{broken error", "{broken error"),
    ("", UNKNOWN),
    (" \n", UNKNOWN),
    ('{"message":"top-level only"}', "top-level only"),
    ('{"error":""}', '{"error":""}'),
    (' {"error":{"code":"no_message"}} ', ' {"error":{"code":"no_message"}} '),
]


def substitute(monkeypatch, raw, status=200, failure=None):
    requests, waits = [], []

    @asynccontextmanager
    async def response(self, client, payload):
        request = client.build_request("POST", "https://test.invalid/chat/completions",
                                       headers={"Authorization": "Bearer " + self.settings.llm_key})
        requests.append(json.loads(json.dumps(payload)))
        if failure:
            raise failure
        yield httpx.Response(status, text=raw, request=request)

    monkeypatch.setattr(TransportClient, "response", response)
    async def wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr("app.analysis.llm.asyncio.sleep", wait)
    return requests, waits


def report(tmp_path, error):
    reference = Diagnostics(tmp_path).record(error.code, error.message, error)
    text = (tmp_path / reference["log_file"].split("/")[-1]).read_text(encoding="utf-8")
    assert f"简要说明：{reference['message']}" in text
    return reference, text


@pytest.mark.parametrize("status,code,count", [
    (400, "model_request_rejected", 1), (401, "model_access_denied", 1),
    (402, "model_access_denied", 1), (403, "model_access_denied", 1),
    (404, "model_request_rejected", 1), (422, "model_request_rejected", 1),
    (408, "model_unavailable", 3), (409, "model_unavailable", 3),
    (429, "model_unavailable", 3), (503, "model_unavailable", 3),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_http_message_preserves_code_chain_and_retries(monkeypatch, tmp_path, status, code, count, streaming):
    raw = '{"error":{"message":"SSL DNS timeout: rejected by provider"}}'
    requests, waits = substitute(monkeypatch, raw, status)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=streaming))
    model.diagnostics = Diagnostics(tmp_path)
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    error = caught.value
    assert (error.code, model.calls, len(requests)) == (code, count, count)
    assert waits == ([3, 3] if count == 3 else [])
    assert isinstance(error.__cause__, httpx.HTTPStatusError)
    assert error.__cause__.response.text == raw
    assert error.message == "SSL DNS timeout: rejected by provider"
    reference, _ = report(tmp_path, error)
    assert reference["message"] == error.message
    assert all("简要说明：SSL DNS timeout: rejected by provider" in p.read_text(encoding="utf-8")
               for p in tmp_path.glob("*.log"))


@pytest.mark.parametrize("field,marker,code,retry", [
    ("code", "Throttling.ServiceOverloaded", "model_unavailable", True),
    ("type", "Throttling.Aborted", "model_unavailable", True),
    ("code", 429, "model_unavailable", True), ("code", "503", "model_unavailable", True),
    ("code", "Unknown.ProviderError", None, True), ("type", "new_error_type", None, True),
    ("code", "401", "model_access_denied", False), ("code", "quota_exceeded", "model_access_denied", False),
    ("type", "authentication_error", "model_access_denied", False),
    ("code", "InvalidApiKey", "model_access_denied", False),
    ("code", "insufficient_quota", "model_access_denied", False),
    ("code", "permission_denied", "model_access_denied", False),
    ("code", "model_not_found", "model_request_rejected", False),
    ("type", "invalid_request_error", "model_request_rejected", False),
    ("code", "unsupported_parameter", "model_request_rejected", False),
])
@pytest.mark.parametrize("kind", ["normal", "normal-top", "stream-data", "stream-event", "stream-top"])
def test_provider_errors_retry_unless_known_permanent(monkeypatch, tmp_path, field, marker, code, retry, kind):
    token = "candidate-private-credential"
    body = {field: marker, "message": "Too many requests. " + token}
    raw = json.dumps({**(body if kind.endswith("-top") else {"error": body}), "request_id": "request-marker"})
    streaming = not kind.startswith("normal")
    content = ("event: error\n" if kind == "stream-event" else "") + "data: " + raw + "\n\n" if streaming else raw
    requests, waits = substitute(monkeypatch, content)
    model = ModelClient(Settings(llm_key=token, free_tier_confirmed=True, llm_stream=streaming))
    model.diagnostics = Diagnostics(tmp_path)
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    error = caught.value
    assert model.calls == len(requests) == (3 if retry else 1)
    assert waits == ([3, 3] if retry else [])
    assert error.code == (code or ("model_stream_error" if streaming else "model_response_invalid"))
    assert error.message == "Too many requests. [REDACTED]"
    assert "request-marker" in str(error.__cause__)
    assert token not in report(tmp_path, error)[1]
    assert all(token not in p.read_text(encoding="utf-8") for p in tmp_path.glob("*.log"))
    assert model.client.is_closed and not model.active_requests and model.usage_reports == 0


@pytest.mark.parametrize("marker", ["Throttling.ServiceOverloaded", "Throttling.Aborted", "unknown_error",
                                   "data_inspection_failed", "content_filter"])
def test_stream_retry_discards_partial_answer_and_closes_response(monkeypatch, marker):
    original_client = httpx.AsyncClient
    replies, waits = [], []
    async def respond(request):
        if not replies:
            content = 'data: {"choices":[{"delta":{"content":"discard this"}}],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
            if marker == "content_filter":
                content += 'data: {"choices":[{"finish_reason":"content_filter"}]}\n\n'
            else:
                message = ("Output data may contain inappropriate content." if marker == "data_inspection_failed"
                           else "Too many requests.")
                content += "data: " + json.dumps({"error": {"code": marker, "message": message}}) + "\n\n"
        else:
            content = 'data: {"choices":[{"delta":{"content":"complete answer"},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
        response = httpx.Response(200, text=content)
        replies.append(response)
        return response
    async def wait(seconds):
        assert replies[-1].is_closed
        waits.append(seconds)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("app.analysis.llm.asyncio.sleep", wait)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=True))
    assert run_model(model, "request", [], max_tokens=1024) == "complete answer"
    assert waits == [3] and model.calls == model.usage_reports == 2
    assert model.usage == {"prompt_tokens": 18, "completion_tokens": 5}
    assert all(response.is_closed for response in replies) and model.client.is_closed
    assert not model.active_requests


@pytest.mark.parametrize("streaming,broken", [
    (False, "{broken response"), (False, "[]"), (False, '{"choices":[null]}'),
    (False, '{"choices":[{"message":{"content":""}}]}'),
    (False, '{"choices":[{"finish_reason":"insufficient_system_resource"}]}'),
    (True, "data: {broken response\n\n"), (True, "data: []\n\n"),
    (True, 'data: {"choices":[null]}\n\n'), (True, 'data: {"choices":[{"delta":42}]}\n\n'),
    (True, 'data: {"choices":[{"delta":{"content":"discard partial"}}]}\n\n'),
    (True, 'data: {"choices":[{"finish_reason":"insufficient_system_resource"}]}\n\n'),
])
def test_invalid_upstream_envelope_or_incomplete_response_can_recover(monkeypatch, streaming, broken):
    requests, waits = [], []
    original_client = httpx.AsyncClient
    responses = []
    async def respond(request):
        good = {"choices": [{"message": {"content": "complete"}, "delta": {"content": "complete"},
                             "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        content = broken if not responses else ("data: " + json.dumps(good) + "\n\n" if streaming else json.dumps(good))
        response = httpx.Response(200, text=content)
        responses.append(response)
        requests.append(json.loads(request.content))
        return response
    # 使用真实请求/响应上下文，复核失败响应会关闭且不会拼接残缺正文。
    async def wait(delay):
        assert responses[-1].is_closed
        waits.append(delay)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("app.analysis.llm.asyncio.sleep", wait)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=streaming))
    assert run_model(model, "request", [], max_tokens=1024) == "complete"
    assert model.calls == 2 and waits == [3] and requests[0] == requests[1]
    assert model.usage_reports == 1 and model.usage == {"prompt_tokens": 7, "completion_tokens": 2}
    assert all(response.is_closed for response in responses) and model.client.is_closed and not model.active_requests


@pytest.mark.parametrize("streaming", [False, True])
def test_length_limit_does_not_retry_same_request(monkeypatch, streaming):
    raw = '{"choices":[{"finish_reason":"length"}]}'
    requests, waits = substitute(monkeypatch, "data: " + raw + "\n\n" if streaming else raw)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=streaming))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == "model_output_truncated" and len(requests) == 1 and waits == []


def test_completed_answer_validation_still_has_one_repair(monkeypatch):
    raw = '{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}'
    requests, waits = substitute(monkeypatch, raw)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=False))
    with pytest.raises(AppError) as caught:
        run_model(model, "generate", "overview", {}, model_validator("overview", lambda value: None))
    assert caught.value.code == "invalid_model_output" and len(requests) == 2 and waits == []
    first, repaired = requests[0]["messages"], requests[1]["messages"]
    assert repaired[:-2] == first
    assert repaired[-2] == {"role": "assistant", "content": "{}"}
    assert repaired[-1]["role"] == "user" and "上次输出未通过校验" in repaired[-1]["content"]


@pytest.mark.parametrize("failure", [RuntimeError("local bug"), OSError("local file error"),
                                    AppError("local_error", "local failure")])
def test_local_errors_do_not_enter_model_retries(monkeypatch, failure):
    requests, waits = substitute(monkeypatch, "", failure=failure)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=False))
    with pytest.raises(type(failure)) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value is failure and len(requests) == 1 and waits == []


@pytest.mark.parametrize("kind", ["http", "normal", "stream-data", "stream-event", "finish-normal", "finish-stream"])
def test_output_filter_has_three_attempts_with_fixed_wait_and_original_report(monkeypatch, tmp_path, kind):
    token = "candidate-private-credential"
    message = "<400> InternalError.Algo.DataInspectionFailed: Output data may contain inappropriate content. " + token
    raw = json.dumps({"error": {"code": "data_inspection_failed", "type": "data_inspection_failed", "message": message},
                      "request_id": "inspection-request"})
    if kind.startswith("finish-"):
        choice = {"finish_reason": "content_filter", "message": {"content": "partial"}, "delta": {"content": "partial"}}
        raw = json.dumps({"choices": [choice], "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
    streaming = kind in {"stream-data", "stream-event", "finish-stream"}
    content = ("event: error\n" if kind == "stream-event" else "") + "data: " + raw + "\n\n" if streaming else raw
    requests, waits = substitute(monkeypatch, content, 400 if kind == "http" else 200)
    model = ModelClient(Settings(llm_key=token, free_tier_confirmed=True, llm_stream=streaming))
    model.diagnostics = Diagnostics(tmp_path)
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == "model_output_filtered"
    assert model.calls == len(requests) == 3 and waits == [3, 3]
    assert all(request == requests[0] for request in requests)
    reference, text = report(tmp_path, caught.value)
    assert reference["message"] == caught.value.message
    if not kind.startswith("finish-"):
        assert "inspection-request" in text and "Output data may contain inappropriate content." in text
    assert all(token not in path.read_text(encoding="utf-8") for path in tmp_path.glob("*.log"))
    assert model.usage_reports == (3 if kind.startswith("finish-") else 0)
    assert model.client.is_closed and not model.active_requests


@pytest.mark.parametrize("marker,message,code,count", [
    ("data_inspection_failed", "Input data may contain inappropriate content.", "model_request_rejected", 1),
    ("unknown_error", "Output data may contain inappropriate content.", "model_stream_error", 3),
])
def test_input_filter_is_permanent_but_unknown_error_retries(monkeypatch, marker, message, code, count):
    raw = json.dumps({"error": {"code": marker, "message": message}})
    requests, waits = substitute(monkeypatch, "data: " + raw + "\n\n")
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=True))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == code
    assert len(requests) == model.calls == count and waits == ([3, 3] if count == 3 else [])


@pytest.mark.parametrize("raw,expected", MESSAGES)
@pytest.mark.parametrize("kind", ["http", "normal", "stream-event"])
def test_message_priority_and_fallback(monkeypatch, tmp_path, raw, expected, kind):
    content = raw
    if kind == "stream-event":
        # 已生成的正常内容不能出现在报错中；支持多行及没有结尾空行的错误事件。
        content = 'data: {"choices":[{"delta":{"content":"NORMAL CONTENT"}}]}\n\nevent: error\n'
        content += "\n".join("data: " + line for line in raw.split("\n"))
    requests, waits = substitute(monkeypatch, content, 400 if kind == "http" else 200)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True, llm_stream=kind == "stream-event"))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    error = caught.value
    assert error.message == expected
    permanent = '"quota_exceeded"' in raw
    assert error.code == ("model_access_denied" if permanent else {
        "http": "model_request_rejected", "normal": "model_response_invalid", "stream-event": "model_stream_error"}[kind])
    count = 1 if permanent or kind == "http" else 3
    assert len(requests) == count and waits == ([3, 3] if count == 3 else [])
    if kind == "stream-event":
        assert isinstance(error.__cause__, RuntimeError)
        assert "NORMAL CONTENT" not in str(error.__cause__)
    assert report(tmp_path, error)[0]["message"] == expected


@pytest.mark.parametrize("raw,expected", MESSAGES[:6] + MESSAGES[10:])
def test_stream_data_error_uses_only_current_event(monkeypatch, tmp_path, raw, expected):
    substitute(monkeypatch, 'data: {"choices":[{"delta":{"content":"NORMAL CONTENT"}}]}\n\n'
               + "data: " + raw + "\n\n")
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == ("model_access_denied" if '"quota_exceeded"' in raw else "model_stream_error")
    assert caught.value.message == expected
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "NORMAL CONTENT" not in str(caught.value.__cause__)
    assert report(tmp_path, caught.value)[0]["message"] == expected


@pytest.mark.parametrize("failure,expected,code", [
    (httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]"), "SSL/TLS 安全连接失败", "model_network_error"),
    (httpx.ConnectError("DNS failure"), "网络连接失败", "model_network_error"),
    (httpx.ReadTimeout("wait"), "请求或处理超时", "model_timeout"),
    (httpx.DecodingError("invalid compressed response"), "模型请求失败", "model_response_invalid"),
])
def test_real_network_failures_keep_existing_retries(monkeypatch, tmp_path, failure, expected, code):
    requests, waits = substitute(monkeypatch, "", failure=failure)
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == code and caught.value.__cause__ is failure
    assert len(requests) == model.calls == 3 and waits == [3, 3]
    assert report(tmp_path, caught.value)[0]["message"].startswith(expected)


@pytest.mark.parametrize("failure,expected", [
    (ssl.SSLCertVerificationError("verify failed"), "SSL/TLS 证书校验失败"),
    (socket.gaierror("DNS failed"), "网络连接失败"),
    (TimeoutError("wait"), "请求或处理超时"),
])
def test_typed_network_cause_takes_precedence(tmp_path, failure, expected):
    error = AppError("model_stream_error", "provider error")
    error.__cause__ = failure
    assert report(tmp_path, error)[0]["message"].startswith(expected)


@pytest.mark.parametrize("raw,expected", MESSAGES)
@pytest.mark.parametrize("status", [200, 403])
def test_model_list_uses_shared_extraction(client, app, monkeypatch, raw, expected, status):
    client.put("/api/v1/settings/model", json={"token": "fake-only"})
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        return httpx.Response(status, text=raw, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    response = client.get("/api/v1/settings/model/options")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "model_list_failed" and error["message"] == expected
    assert len(requests) == 1
    text = (app.state.pipeline.get_settings().log_dir / error["log_file"].split("/")[-1]).read_text(encoding="utf-8")
    assert f"简要说明：{expected}" in text
    if status == 403:
        assert "HTTPStatusError" in text
    else:
        assert "KeyError" in text or "JSONDecodeError" in text


@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), "SSL/TLS 证书校验失败"),
    (httpx.ConnectError("DNS failure"), "网络连接失败"),
    (httpx.ReadTimeout("wait"), "请求或处理超时"),
])
def test_model_list_network_failure_keeps_category(client, app, monkeypatch, failure, expected):
    client.put("/api/v1/settings/model", json={"token": "fake-only"})
    requests = []

    def get(*args, **kwargs):
        requests.append(args)
        raise failure

    monkeypatch.setattr(httpx, "get", get)
    error = client.get("/api/v1/settings/model/options").json()["error"]
    assert error["code"] == "model_list_failed" and error["message"].startswith(expected)
    assert len(requests) == 1


def test_message_parse_failure_falls_back_without_hiding_http_exception(monkeypatch, tmp_path):
    from app.errors import model_error_message

    def invalid(raw):
        raise ValueError("cannot parse")

    monkeypatch.setattr("app.errors.json.loads", invalid)
    assert model_error_message("raw provider body") == "raw provider body"
    request = httpx.Request("POST", "https://test.invalid")
    response = httpx.Response(403, text="raw provider body", request=request)
    error = httpx.HTTPStatusError("denied", request=request, response=response)
    reference = Diagnostics(tmp_path).record("model_access_denied", "old generic", error)
    assert reference["message"] == "raw provider body"
    assert "HTTPStatusError" in (tmp_path / reference["log_file"].split("/")[-1]).read_text(encoding="utf-8")


def test_unread_http_body_does_not_break_report(tmp_path):
    request = httpx.Request("POST", "https://test.invalid")
    response = httpx.Response(403, stream=httpx.ByteStream(b"not read"), request=request)
    error = httpx.HTTPStatusError("denied", request=request, response=response)
    reference = Diagnostics(tmp_path).record("model_access_denied", "old generic", error)
    assert reference["message"] == UNKNOWN and reference["log_saved"] is True


def test_multiline_error_event_ends_before_next_generation(monkeypatch, tmp_path):
    substitute(monkeypatch, 'event: error\ndata: {\ndata: "message": "provider detail"\ndata: }\n\n'
               'data: {"choices":[{"delta":{"content":"LATER CONTENT"}}]}\n\n')
    model = ModelClient(Settings(llm_key="fake-only", free_tier_confirmed=True))
    with pytest.raises(AppError) as caught:
        run_model(model, "request", [], max_tokens=1024)
    assert caught.value.code == "model_stream_error" and caught.value.message == "provider detail"
    reference, text = report(tmp_path, caught.value)
    assert reference["message"] == "provider detail" and "LATER CONTENT" not in text


@pytest.mark.parametrize("kind", ["http", "normal", "stream"])
def test_connection_test_redacts_unsaved_token(client, app, monkeypatch, kind):
    token = "candidate-private-credential"
    raw = json.dumps({"error": {"message": "SSL DNS timeout " + token}})
    requests, _ = substitute(monkeypatch, "data: " + raw + "\n\n" if kind == "stream" else raw,
                             403 if kind == "http" else 200)
    response = client.post("/api/v1/settings/model/test", json={"token": token, "free_tier_confirmed": True,
                                                              "stream": kind == "stream"})
    error = response.json()["error"]
    assert error["message"] == "SSL DNS timeout [REDACTED]"
    text = (app.state.pipeline.get_settings().log_dir / error["log_file"].split("/")[-1]).read_text(encoding="utf-8")
    assert token not in text and token not in response.text and len(requests) == (1 if kind == "http" else 3)
    assert not (app.state.pipeline.get_settings().data_dir / "_private/model-settings.json").exists()


def test_task_error_and_log_use_same_raw_message(client, app, video_file, monkeypatch):
    requests, _ = substitute(monkeypatch, '{"error":{"message":"SSL DNS timeout provider message"}}', 403)
    video = upload(client, video_file)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    settings = replace(app.state.pipeline.get_settings(), llm_key="fake-only", free_tier_confirmed=True, llm_stream=False)
    pipeline = Pipeline(lambda snapshot=settings: snapshot, app.state.sessions, app.state.diagnostics, FakeRecognizer, ModelClient)
    pipeline.run(task_id)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "failed" and len(requests) == 1
    assert task["error"]["message"] == "SSL DNS timeout provider message"
    text = (settings.log_dir / task["error"]["log_file"].split("/")[-1]).read_text(encoding="utf-8")
    assert f"简要说明：{task['error']['message']}" in text
