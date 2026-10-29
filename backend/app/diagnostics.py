"""每次错误保存独立的易读日志；密钥和请求输入不写入日志。"""
import json
import errno
import logging
import re
import socket
import ssl
import subprocess
import traceback
import uuid
from datetime import datetime, timedelta, timezone
import httpx
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
from .errors import AppError, model_error_message
from .error_codes import MODEL_ERRORS, KNOWN_ERRORS


def error_log_location(reference):
    return f"详细日志：{reference['log_file']}（错误编号 {reference['error_id']}）。" if reference.get("log_file") else "错误日志未能写入，请检查 log 文件夹权限和磁盘空间。"


# 遍历异常原因和异常组并去重，保留真正失败来源以生成统一摘要。
def exception_chain(exc):
    pending, seen = [exc] if exc is not None else [], set()
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        yield cause
        if isinstance(cause, BaseExceptionGroup):
            pending.extend(reversed(cause.exceptions))
        following = cause.__cause__ or (cause.__context__ if not cause.__suppress_context__ else None)
        if following is not None:
            pending.append(following)


def error_summary(code, message, causes):
    """真实网络异常使用预设说明；模型接口返回的报错保留原文。"""
    if any(isinstance(cause, ssl.SSLCertVerificationError) or
           isinstance(cause, httpx.TransportError) and "CERTIFICATE_VERIFY_FAILED" in str(cause) for cause in causes):
        return "SSL/TLS 证书校验失败，请查看错误报告。"
    if any(isinstance(cause, ssl.SSLError) or
           isinstance(cause, httpx.TransportError) and "[SSL:" in str(cause) for cause in causes):
        return "SSL/TLS 安全连接失败，请查看错误报告。"
    if any(isinstance(cause, (httpx.TimeoutException, TimeoutError, subprocess.TimeoutExpired)) for cause in causes):
        return "请求或处理超时，请查看错误报告。"
    if any(isinstance(cause, (httpx.TransportError, ConnectionError, socket.gaierror)) for cause in causes):
        return "网络连接失败，请查看错误报告。"
    for cause in causes:
        if isinstance(cause, AppError) and cause.code in MODEL_ERRORS:
            return cause.message
    if code in MODEL_ERRORS:
        for cause in causes:
            if isinstance(cause, httpx.HTTPStatusError):
                try:
                    return model_error_message(cause.response.text)
                except httpx.ResponseNotRead:
                    return model_error_message("")
        return message
    for cause in causes:
        if isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
            if status in {401, 402, 403}:
                return "外部服务拒绝访问，请检查凭据、权限或额度，详情见错误报告。"
            if status == 429:
                return "外部服务请求过于频繁，请稍后重试，详情见错误报告。"
            if status >= 500:
                return "外部服务暂时不可用，请稍后重试，详情见错误报告。"
            return "外部服务拒绝了请求，请查看错误报告。"
    if any(isinstance(cause, PermissionError) for cause in causes):
        return "本地文件访问权限不足，请查看错误报告。"
    if any(isinstance(cause, OSError) and cause.errno == errno.ENOSPC for cause in causes):
        return "本地磁盘空间不足，请查看错误报告。"
    if any(isinstance(cause, FileNotFoundError) for cause in causes):
        return "所需文件或运行工具不存在，请查看错误报告。"
    if any(isinstance(cause, OSError) for cause in causes):
        return "本地文件读写失败，请查看错误报告。"
    if any(isinstance(cause, subprocess.CalledProcessError) for cause in causes):
        return "外部处理程序执行失败，请查看错误报告。"
    if code == "invalid_input":
        return "输入格式错误，请检查填写内容，详情见错误报告。"
    if code == "model_output_validation":
        return "模型输出的结构或引用校验未通过，正在自动修正一次。"
    if any(isinstance(cause, (ValidationError, RequestValidationError, json.JSONDecodeError)) for cause in causes):
        return "数据格式或校验错误，请查看错误报告。"
    # 这些业务错误已有明确的预设提示，保留操作方法；未知程序异常不推测原因。
    for cause in causes:
        if isinstance(cause, AppError) and cause.code in KNOWN_ERRORS:
            return cause.message
    if code in KNOWN_ERRORS:
        return message
    return {
        "frontend_error": "页面出现错误，请查看错误报告。",
        "request_network_error": "无法连接本地服务，请检查服务是否运行，详情见错误报告。",
        "response_format_error": "服务返回的数据格式异常，请查看错误报告。",
        "http_error": "请求未能完成，请查看错误报告。",
    }.get(code, "出现了未知错误，请查看错误报告。")


class ClientErrorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(max_length=2000)
    stack: str = ""
    location: str = Field(default="", max_length=300)
    code: Literal["frontend_error", "request_network_error", "response_format_error", "http_error"] = "frontend_error"
    request_url: str = Field(default="", max_length=500)
    request_method: str = Field(default="", max_length=10)
    http_status: int | None = Field(default=None, ge=100, le=599)
    response_body: str = ""


class Diagnostics:
    def __init__(self, directory, secret=lambda: ""):
        self.directory = directory
        self.secret = secret

    # 同时清理当前凭据与已知敏感模式，日志和错误文案共用脱敏规则。
    def redact(self, text, secrets=()):
        configured = self.secret()
        configured = (configured,) if isinstance(configured, str) else configured
        for secret in (*configured, *secrets):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(\b(?:bearer|basic)\s+)[^\s\"',}]+", r"\1[REDACTED]", text)
        text = re.sub(r"""(?i)((?:token|api[_-]?key|authorization)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)""", r"\1[REDACTED]", text)
        return re.sub(r"\b(?:hf_|sk-|ms-)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)

    # 为每次错误生成独立编号和日志引用，写日志失败也返回明确的可读状态。
    def record(self, code, message, exc=None, **context):
        error_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone(timedelta(hours=8)))
        filename = f"{now:%Y-%m-%d_%H-%M-%S}_{error_id}.log"
        stack = context.pop("stack", "")
        causes = list(exception_chain(exc))
        secrets = []
        for cause in causes:
            if isinstance(cause, (httpx.RequestError, httpx.HTTPStatusError)):
                try:
                    authorization = cause.request.headers.get("authorization", "")
                    if authorization:
                        secrets.append(authorization.split(" ", 1)[-1])
                except RuntimeError:
                    pass  # 未绑定请求的测试异常也能生成报告。
        summary = self.redact(error_summary(code, message, causes), secrets)
        reference = {"code": code, "message": summary, "error_id": error_id}
        if summary != message:
            context = {"operation": message, **context}
        def redact_value(value):
            if isinstance(value, str):
                return self.redact(value, secrets)
            if isinstance(value, dict):
                return {key: "[REDACTED]" if re.fullmatch(r"(?i)token|api[_-]?key|authorization", str(key)) else redact_value(item)
                        for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact_value(item) for item in value]
            return value
        sections = ["StudyBuddy 错误日志", f"时间：{now:%Y-%m-%d %H:%M:%S}（北京时间 UTC+08:00）",
                    f"错误编号：{error_id}", f"错误代码：{code}", f"简要说明：{summary}",
                    "\n【诊断信息】", json.dumps(redact_value(context), ensure_ascii=False, indent=2, default=str)]
        if exc is not None:
            traces = []
            for cause in causes:
                description = json.dumps([{"field": item["loc"], "type": item["type"]} for item in cause.errors()], ensure_ascii=False, indent=2) if isinstance(cause, (ValidationError, RequestValidationError)) else str(cause)
                traces.append("".join(traceback.format_tb(cause.__traceback__)) + f"{type(cause).__name__}: {description}\n")
                if isinstance(cause, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                    for name, value in (("标准输出", cause.stdout), ("标准错误", cause.stderr)):
                        if value:
                            sections.extend([f"\n【外部程序原始{name}】", value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)])
                if isinstance(cause, httpx.HTTPStatusError):
                    response = cause.response
                    try:
                        body = response.text
                    except httpx.ResponseNotRead:
                        body = "响应流未保留正文，请查看异常链中的连接信息。"
                    sections.extend(["\n【HTTP 接口原始报错】", f"HTTP 状态：{response.status_code}",
                                     f"接口地址：{cause.request.url}", "原始响应正文：", body])
            sections.extend(["\n【原始错误与完整异常链】", "\n由以下异常引起（Caused by）：\n".join(traces)])
        if stack:
            sections.extend(["\n【浏览器原始错误与完整堆栈】", stack])
        if exc is None and not stack:
            sections.extend(["\n【原始错误】", "此记录没有异常堆栈，请结合上述诊断信息定位。"])
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / filename).open("x", encoding="utf-8", errors="backslashreplace", newline="\n") as file:
                file.write(self.redact("\n".join(sections), secrets) + "\n")
        except OSError:
            logging.getLogger("studybuddy").error("error_id=%s log_write_failed", error_id)
            return {**reference, "log_saved": False}
        return {**reference, "log_saved": True, "log_file": f"log/{filename}"}

    def close(self):
        # 每次 record 都关闭自己的文件，保留现有服务关闭接口。
        pass
