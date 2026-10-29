import asyncio
import json
import pytest
from app.videos.video_service import VideoService


@pytest.mark.parametrize("origin", ["https://example.com", "null", ""])
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/videos"), ("POST", "/api/v1/videos/missing/analyses"),
    ("POST", "/api/v1/tasks/missing/stop"), ("PATCH", "/api/v1/videos/missing"),
    ("DELETE", "/api/v1/videos/missing"), ("PUT", "/api/v1/settings/model"),
])
def test_all_writes_reject_external_origin(client, origin, method, path):
    response = client.request(method, path, headers={"origin": origin})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("origin", [None, "http://testserver", "http://localhost:5173", "http://127.0.0.1:5173"])
def test_local_write_origins_reach_handler(client, origin):
    response = client.post("/api/v1/tasks/missing/stop", headers={"origin": origin} if origin else {})
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


def test_success_security_headers(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("declared", [None, b"1"])
def test_chunked_upload_limit_before_registration(app, monkeypatch, declared):
    settings = app.state.pipeline.get_settings()
    monkeypatch.setattr(settings, "max_upload_bytes", 1)
    registered = []
    monkeypatch.setattr(VideoService, "register_video", lambda *args: registered.append(args))

    async def run():
        headers = [(b"host", b"testserver"), (b"content-type", b"multipart/form-data; boundary=boundary")]
        if declared is not None:
            headers.append((b"content-length", declared))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/api/v1/videos", "raw_path": b"/api/v1/videos", "query_string": b"",
                 "root_path": "", "headers": headers, "server": ("testserver", 80), "client": ("127.0.0.1", 1)}
        chunks = iter([b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.mp4"\r\n'
                       b'Content-Type: video/mp4\r\n\r\n', b"x" * (512 * 1024), b"x" * (512 * 1024),
                       b"x" * 100, b"\r\n--boundary--\r\n"])
        finished = asyncio.Event()
        messages = []
        async def receive():
            try:
                return {"type": "http.request", "body": next(chunks), "more_body": True}
            except StopIteration:
                await finished.wait()
                return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished.set()
        await asyncio.wait_for(app(scope, receive, send), 3)
        return messages

    messages = asyncio.run(run())
    assert messages[0]["status"] == 413
    headers = dict(messages[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff" and headers[b"x-request-id"]
    assert json.loads(b"".join(m.get("body", b"") for m in messages[1:]))["error"]["error_id"]
    assert registered == []


@pytest.mark.parametrize("code", ["model_input_budget", "processing_failed", "invalid_checkpoint", "media_cancelled",
    "document_missing", "document_task_missing", "document_busy", "document_not_prepared", "document_content_missing",
    "document_source_changed", "document_generation_changed", "document_generation_limit", "document_request_limit",
    "document_limit", "document_encoding", "document_dependency", "encrypted_document", "invalid_document",
    "invalid_document_task_state", "notes_unavailable"])
def test_registered_business_error_keeps_actionable_message(code):
    from app.errors import AppError
    from app.diagnostics import error_summary
    message = "保留已有检查点，请继续或重新生成。"
    assert error_summary(code, message, [AppError(code, message)]) == message
    assert error_summary("unknown_program_error", "untrusted", []) == "出现了未知错误，请查看错误报告。"
