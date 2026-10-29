import subprocess
from pathlib import Path
from app.diagnostics import Diagnostics


def log_text(directory, reference):
    return (directory / reference["log_file"].split("/")[-1]).read_text(encoding="utf-8")


def test_api_error_has_reference_and_server_traceback(client, app):
    response = client.get("/api/v1/videos/missing/content")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["log_file"].endswith(error["error_id"] + ".log")
    assert len(error["error_id"]) == 12
    content = log_text(app.state.pipeline.get_settings().log_dir, error)
    assert content.startswith("StudyBuddy 错误日志")
    assert error["error_id"] in content
    assert "AppError" in content
    assert response.headers["x-request-id"] in content
    assert f"简要说明：{error['message']}" in content


def test_unhandled_error_is_simple_and_logged(client, app):
    @app.get("/api/v1/test-crash")
    def crash():
        raise RuntimeError("internal debug detail")
    response = client.get("/api/v1/test-crash")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == "出现了未知错误，请查看错误报告。"
    assert "internal debug detail" not in response.text
    content = log_text(app.state.pipeline.get_settings().log_dir, response.json()["error"])
    assert "internal debug detail" in content


def test_validation_and_client_log_never_store_token(client, app):
    secret = "my-private-token-for-test"
    client.put("/api/v1/settings/model", json={"token": secret})
    invalid = client.put("/api/v1/settings/model", json={"base_url": "https://user:" + secret + "@invalid.test/v1", "token": secret})
    assert invalid.status_code == 422
    response = client.post("/api/v1/diagnostics/client-errors", json={
        "message": f"failure {secret} token=another-private-value", "stack": "Error at main.tsx:20", "location": "/#settings"})
    assert response.status_code == 201
    content = log_text(app.state.pipeline.get_settings().log_dir, response.json())
    assert secret not in content
    assert "another-private-value" not in content
    assert "[REDACTED]" in content
    assert "main.tsx:20" in content
    assert client.post("/api/v1/diagnostics/client-errors", headers={"origin": "https://invalid.test"}, json={"message": "oops"}).status_code == 403


def test_ffmpeg_stderr_is_kept_in_detailed_log(tmp_path):
    log = Diagnostics(tmp_path)
    try:
        try:
            raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"Invalid data found when processing input")
        except subprocess.CalledProcessError as cause:
            try:
                raise RuntimeError("media failed") from cause
            except RuntimeError as error:
                reference = log.record("media_failed", "媒体处理失败", error, stage="checking")
        content = log_text(tmp_path, reference)
        assert "Invalid data found when processing input" in content
        assert "CalledProcessError" in content
    finally:
        log.close()


def test_log_disk_failure_is_not_reported_as_saved(tmp_path, monkeypatch):
    log = Diagnostics(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("no space left")
    original_open = Path.open
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: fail() if path.parent == tmp_path else original_open(path, *args, **kwargs))
    try:
        result = log.record("test", "模拟磁盘故障")
        assert result["log_saved"] is False
        assert "log_file" not in result
    finally:
        log.close()


def test_chained_validation_error_hides_input(tmp_path):
    from pydantic import BaseModel, ValidationError
    class Schema(BaseModel):
        count: int
    log = Diagnostics(tmp_path)
    try:
        try:
            Schema(count="unrecognized-secret-in-input")
        except ValidationError:
            try:
                raise RuntimeError("invalid output")
            except RuntimeError as exc:
                reference = log.record("test", "模型输出校验失败", exc)
        text = log_text(tmp_path, reference)
        # source code lines are included in a traceback; check serialized exception fields, not this test's literal.
        assert "input_value=" not in text
        assert '"type": "int_parsing"' in text
    finally:
        log.close()
