import uuid
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from .errors import AppError
from .documents.extraction import MAX_DOCUMENT_BYTES


# 集中安装请求编号、上传边界及错误响应，让所有 API 共用诊断契约。
def configure_http(app, get_settings, diagnostics):
    def error_response(request, code, message, status, exc=None, **details):
        reference = diagnostics.record(code, message, exc, request_id=getattr(request.state, "request_id", ""),
                                       method=request.method, path=request.url.path, **details)
        return JSONResponse({"error": reference}, status_code=status,
                            headers={"X-Request-ID": getattr(request.state, "request_id", ""),
                                     "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @app.exception_handler(AppError)
    async def handle_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status, exc)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        message = str(exc.detail) if isinstance(exc.detail, str) else "请求无法处理，请检查输入。"
        return error_response(request, "http_error", message, exc.status_code, exc)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        protected = request.url.path.startswith(("/api/v1/settings", "/api/v1/diagnostics", "/api/v1/video-downloads", "/api/v1/bilibili"))
        if protected or request.url.path.startswith("/api/v1/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), "http://localhost:5173", "http://127.0.0.1:5173"}
            if origin is not None and origin not in allowed:
                return error_response(request, "local_only", "设置与日志接口仅允许本机页面访问。", 403)
        # 在 multipart 临时文件解析前限制请求体，不能仅在保存原件时限流。
        settings = get_settings()
        limit = settings.max_upload_bytes + 1024 * 1024
        if request.url.path == "/api/v1/documents":
            limit = min(MAX_DOCUMENT_BYTES, settings.max_upload_bytes) + 1024 * 1024
        if request.method == "POST" and request.url.path in {"/api/v1/videos", "/api/v1/documents"}:
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                return error_response(request, "invalid_length", "上传长度无效。", 400)
            if declared > limit:
                return error_response(request, "upload_limit", "文件超过大小限制。", 413)
            original_receive = request._receive
            received = 0
            async def bounded_receive():
                nonlocal received
                message = await original_receive()
                received += len(message.get("body", b""))
                if received > limit:
                    request.state.upload_exceeded = True
                    from starlette.exceptions import HTTPException
                    raise HTTPException(413, "文件超过大小限制。")
                return message
            request._receive = bounded_receive
        try:
            response = await call_next(request)
        except Exception as exc:
            response = error_response(request, "internal_error", "服务处理失败，请稍后重试。", 500, exc)
        if getattr(request.state, "upload_exceeded", False):
            # multipart 解析器可能把 receive 的异常包装为 400；保留真实的超限状态。
            response = error_response(request, "upload_limit", "文件超过大小限制。", 413)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith(("/api/v1/settings", "/api/v1/bilibili")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # Pydantic 的默认错误包含输入值，设置请求可能带密钥。
        return error_response(request, "invalid_input", "输入格式错误，请检查填写内容。", 422, exc,
                              fields=[{"location": item["loc"], "type": item["type"]} for item in exc.errors()])
