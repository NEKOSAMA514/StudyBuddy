from ..error_codes import REQUEST_RETRY_ERRORS
import asyncio
from .callbacks import ChapterCallbacks
import json
import logging
import time
import uuid
from time import monotonic
from contextlib import asynccontextmanager
from contextvars import ContextVar
import httpx
from pydantic import ValidationError
from ..errors import AppError, model_error_message
from ..diagnostics import Diagnostics
from .budgets import message_characters
from .markdown import parse_chapters
from .activity import emit

logger = logging.getLogger("studybuddy")
request_context = ContextVar("model_request_context", default=("connection_test", []))
activity_context = ContextVar("model_activity_context", default=None)


# 所有并发请求、重试和修正共用一个发送间隔，限制实际发出请求的频率。
class RequestPacer:
    def __init__(self, interval):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_send = 0.0

    async def wait(self):
        async with self.lock:
            delay = self.next_send - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_send = monotonic() + self.interval


class ModelClient:
    """共用请求、发送间隔及输出校验机制。"""
    missing_key_message = "请打开模型设置填写当前 API 的密钥，再重试任务。"

    def messages(self, operation, payload):
        raise NotImplementedError

    def input_characters(self, operation, payload):
        return message_characters(self.messages(operation, payload))

    def __init__(self, settings):
        self.settings = settings
        self.calls = 0
        self.cancelled_calls = 0
        self.usage_reports = 0
        self.active_requests = {}
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.diagnostics = None
        self.check_stop = lambda: None
        self.before_request = lambda: None
        self.activity = None

    def notify(self, state, **details):
        emit(self, state, **{**(activity_context.get() or {}), **details})

    async def __aenter__(self):
        self.pacer = RequestPacer(self.settings.llm_request_interval_seconds)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False,
            limits=httpx.Limits(max_connections=self.settings.llm_section_concurrency,
                               max_keepalive_connections=self.settings.llm_section_concurrency))
        return self

    async def __aexit__(self, *exc):
        try:
            await self.client.aclose()
        except Exception as cleanup:
            if exc[0] is None:
                raise
            if self.diagnostics:
                self.diagnostics.record("model_cleanup_failed", "模型连接清理失败，保留原始退出原因。", cleanup)

    # 为本次实际请求记录动态和用量，取消时保留计数并向上层传播。
    async def request(self, messages, max_tokens, *, json_mode=None):
        context = activity_context.get()
        token = None
        if context is None:
            operation, ids = request_context.get()
            token = activity_context.set({"request_id": uuid.uuid4().hex, "operation": operation, "chapter_ids": ids})
        try:
            result = await self._request(messages, max_tokens, json_mode=json_mode)
            attempt = self.active_requests.get(asyncio.current_task())
            if attempt:
                attempt["status"] = "completed"
            self.notify("validating")
            return result
        except asyncio.CancelledError:
            attempt = self.active_requests.get(asyncio.current_task())
            if attempt and attempt["status"] == "running":
                self.cancelled_calls += 1
            logger.info("operation=%s chapters=%s request_cancelled=true usage=%s", *request_context.get(),
                        (attempt or {}).get("usage") or "unknown")
            raise
        finally:
            self.active_requests.pop(asyncio.current_task(), None)
            if token is not None:
                self.notify("request_done")
                activity_context.reset(token)

    # 先检查输入和凭据，再按限速发送；只有明确可重试的请求错误最多尝试三次。
    async def _request(self, messages, max_tokens, *, json_mode=None):
        if message_characters(messages) > self.settings.llm_input_text_limit:
            raise AppError("model_input_budget", "请求超过生成输入上限，成功检查点已保留。")
        if not self.settings.llm_key:
            raise AppError("model_not_configured", self.missing_key_message)
        payload = {"model": self.settings.llm_model, "messages": messages, "temperature": 0.2,
                   "max_tokens": max_tokens, "stream": self.settings.llm_stream}
        if self.settings.llm_json_mode if json_mode is None else json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.llm_provider == "bailian":
            payload["enable_thinking"] = False
        retry_delay = self.settings.llm_request_interval_seconds
        for attempt in range(3):
            self.check_stop()
            context = activity_context.get()
            if context is not None:
                context["network_attempt"] = attempt + 1
            self.notify("pacing")
            await self.pacer.wait()
            self.check_stop()
            try:
                self.before_request()
                self.calls += 1
                logger.info("operation=%s chapters=%s request_sent=%s attempt=%s", *request_context.get(), self.calls, attempt + 1)
                request_stats = {"status": "running", "usage": None}
                self.active_requests[asyncio.current_task()] = request_stats
                self.notify("waiting")
                async with self.response(self.client, payload) as response:
                    if response.is_error:
                        # 流式错误也先读取正文，让最终通知指向的日志保留原始报错。
                        await response.aread()
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as cause:
                            code = ("model_access_denied" if response.status_code in {401, 402, 403} else
                                    "model_request_rejected" if response.status_code in {400, 404, 405, 413, 415, 422} else
                                    "model_unavailable")
                            self.response_error(code, response.text, cause)
                    body = None
                    try:
                        if self.settings.llm_stream:
                            return await self.read_stream(response)
                        body = response.json()
                        if not isinstance(body, dict):
                            raise ValueError("invalid response object")
                        if body.get("error") or not body.get("choices") and any(
                                field in body for field in ("error", "message", "code", "type")):
                            self.response_error("model_response_invalid", response.text)
                        self.record_usage(body.get("usage"))
                        choice = body["choices"][0]
                        if not isinstance(choice, dict):
                            raise ValueError("invalid response choice")
                        self.check_finish(choice.get("finish_reason"))
                        content = choice["message"]["content"]
                        if not isinstance(content, str) or not content.strip():
                            raise ValueError("empty content")
                        return content
                    except (ValueError, KeyError, TypeError, IndexError) as exc:
                        message = "模型接口没有返回完整的最终答案；不会将推理过程当成总结。"
                        if not self.settings.llm_stream and (not isinstance(body, dict) or not body.get("choices")):
                            message = self.redact_error(model_error_message(response.text))
                        raise AppError("model_response_invalid", message) from exc
            except AppError as exc:
                # 只重试请求边界上的上游错误；配置错误、长度拆分和正文校验另行处理。
                if exc.code not in REQUEST_RETRY_ERRORS or attempt == 2:
                    raise
                reference = None
                message = (f"模型输出被平台拦截，{retry_delay}秒后重试。" if exc.code == "model_output_filtered"
                           else f"模型请求失败，{retry_delay}秒后重试。")
                if self.diagnostics:
                    reference = self.diagnostics.record(exc.code, message, exc,
                                            attempt=attempt + 1, retry_delay_seconds=retry_delay, model=self.settings.llm_model)
                self.notify("retry_wait", message=message, level="warning", error=reference)
                request_stats["status"] = "retry_wait"
                await asyncio.sleep(retry_delay)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                code = ("model_timeout" if isinstance(exc, httpx.TimeoutException) else
                        "model_response_invalid" if isinstance(exc, httpx.DecodingError) else "model_network_error")
                if attempt == 2:
                    raise AppError(code, "模型请求失败；识别结果已保存，请查看错误报告。") from exc
                reference = None
                if self.diagnostics:
                    reference = self.diagnostics.record(code, f"模型请求失败，{retry_delay}秒后重试。", exc,
                                            attempt=attempt + 1, retry_delay_seconds=retry_delay, model=self.settings.llm_model)
                self.notify("retry_wait", message=f"模型请求失败，{retry_delay}秒后重试。", level="warning", error=reference)
                request_stats["status"] = "retry_wait"
                await asyncio.sleep(retry_delay)

    @asynccontextmanager
    async def response(self, client, payload):
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        kwargs = {"headers": {"Authorization": f"Bearer {self.settings.llm_key}"}, "json": payload}
        if self.settings.llm_stream:
            async with client.stream("POST", url, **kwargs) as response:
                yield response
        else:
            yield await client.post(url, **kwargs)

    def record_usage(self, usage):
        if isinstance(usage, dict) and any(usage.get(key) is not None for key in self.usage):
            request_stats = self.active_requests.get(asyncio.current_task())
            previous = (request_stats or {}).get("usage") or {}
            if not request_stats or request_stats["usage"] is None:
                self.usage_reports += 1
            for key in self.usage:
                self.usage[key] += int(usage.get(key) or 0) - int(previous.get(key) or 0)
            if request_stats:
                request_stats["usage"] = dict(usage)

    def redact_error(self, text):
        # 连接测试使用的 Token 可能尚未保存，也必须从错误消息和诊断正文中隐藏。
        diagnostics = self.diagnostics or Diagnostics(self.settings.log_dir)
        return diagnostics.redact(text, (self.settings.llm_key,))

    def response_error(self, code, raw, cause=None):
        try:
            body = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            body = None
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                markers = {str(error.get(field, "")).lower() for field in ("code", "type")}
                if markers & {"401", "402", "403", "invalid_api_key", "invalidapikey", "authentication_error",
                              "permission_denied", "accessdenied", "quota_exceeded", "insufficient_quota"}:
                    code = "model_access_denied"
                elif markers & {"400", "404", "405", "413", "415", "422", "invalid_request_error",
                                "invalid_parameter", "invalidparameter", "unsupported_parameter", "model_not_found"}:
                    code = "model_request_rejected"
                elif "data_inspection_failed" in markers:
                    code = ("model_output_filtered" if "output data may contain inappropriate content" in
                            str(error.get("message", "")).lower() else "model_request_rejected")
                elif any(marker.startswith("throttling.") or marker in {"rate_limit_exceeded", "rate_limit_error"} or
                         marker.isdigit() and (int(marker) == 429 or 500 <= int(marker) < 600) for marker in markers):
                    code = "model_unavailable"
        raise AppError(code, self.redact_error(model_error_message(raw))) from (cause or RuntimeError(
            "模型接口原始报错：" + self.redact_error(raw)))

    @staticmethod
    def check_finish(reason):
        if reason == "length":
            raise AppError("model_output_truncated", "模型输出达到长度上限，答案被截断；请更换合适模型或减少单次输入。")
        if reason == "content_filter":
            raise AppError("model_output_filtered", "模型输出被平台拦截，答案未完成。")
        if reason == "insufficient_system_resource":
            raise AppError("model_incomplete", "模型没有完成答案生成，请检查平台状态后重试。")

    # 逐行读取最终答案和用量，拒绝截断、过滤及超时响应，不保存推理内容。
    async def read_stream(self, response):
        parts, finished = [], False
        error_data = None
        deadline = time.monotonic() + 300
        async for line in response.aiter_lines():
            if time.monotonic() > deadline:
                raise AppError("model_timeout", "模型流式响应超过五分钟，已停止等待。")
            if not line and error_data is not None:
                self.response_error("model_stream_error", "\n".join(error_data))
            if line.startswith("event:"):
                if error_data is not None:
                    self.response_error("model_stream_error", "\n".join(error_data))
                error_data = [] if line[6:].strip() == "error" else None
                continue
            if not line.startswith("data:"):
                continue
            if error_data is not None:
                error_data.append(line[6:] if line.startswith("data: ") else line[5:])
                continue
            raw_data = line[6:] if line.startswith("data: ") else line[5:]
            data = raw_data.strip()
            if data == "[DONE]":
                finished = True
                break
            if not data:
                continue
            body = json.loads(data)
            if not isinstance(body, dict):
                raise ValueError("invalid stream response object")
            if body.get("error") or not body.get("choices") and any(
                    field in body for field in ("error", "message", "code", "type")):
                self.response_error("model_stream_error", raw_data)
            self.record_usage(body.get("usage"))
            for choice in body.get("choices") or []:
                if not isinstance(choice, dict):
                    raise ValueError("invalid stream choice")
                if choice.get("index", 0) != 0:
                    continue
                reason = choice.get("finish_reason")
                self.check_finish(reason)
                if reason == "stop":
                    finished = True
                # reasoning_content 不属于最终答案，不保存、不展示、不送入下一轮。
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise ValueError("invalid stream delta")
                content = delta.get("content")
                if isinstance(content, str):
                    if content and not parts:
                        self.notify("receiving")
                    if not content:
                        continue
                    parts.append(content)
        if error_data is not None:
            self.response_error("model_stream_error", "\n".join(error_data))
        if not finished or not "".join(parts).strip():
            raise ValueError("incomplete stream")
        return "".join(parts)



    async def generate(self, operation, payload, validate):
        ids = [item["chapter_id"] for item in payload.get("chapters", [])] if operation == "chapters" else [
            payload["chapter"]["chapter_id"]] if operation == "extract" else []
        token = request_context.set((operation, ids))
        activity_token = activity_context.set({"request_id": uuid.uuid4().hex, "operation": operation, "chapter_ids": ids})
        try:
            result = await self.generate_stage(operation, payload, validate)
            context = activity_context.get() or {}
            self.notify("validated", **({"message": "修正或重试后的结果已通过校验，等待保存"}
                                        if context.get("repair") or context.get("network_attempt", 1) > 1 else {}))
            return result
        finally:
            self.notify("request_done")
            activity_context.reset(activity_token)
            request_context.reset(token)

    # JSON 阶段校验结构和业务规则，首次无效时只执行一次结构修正。
    async def generate_stage(self, operation, payload, validate):
        if operation == "chapters":
            return await self.generate_chapters(payload, validate)
        messages = self.messages(operation, payload)
        for repair in range(2):
            self.check_stop()
            if activity_context.get() is not None:
                activity_context.get()["repair"] = repair
            content = await self.stage_request(operation, messages)
            try:
                parsed = self.schemas[operation].model_validate_json(self.unwrap(content)).model_dump()
                return validate(parsed) or parsed
            except (ValidationError, ValueError) as exc:
                if repair:
                    raise AppError("invalid_model_output", "模型结构无效，修正一次后仍失败。") from exc
                self.validation_diagnostic(operation, payload, exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "上次输出未通过校验：" + str(exc) +
                                 "。只修正上述结构或真实来源错误，返回符合Schema的完整JSON。"})
                size = message_characters(messages)
                limit = self.settings.llm_input_text_limit
                if size > limit:
                    raise AppError("model_input_budget", "结构修正请求超过输入预算，成功检查点已保留。") from exc

    # 只解包完整代码围栏，畸形围栏交由现有结构修正流程处理。
    @staticmethod
    def unwrap(content):
        content = content.strip()
        if content.startswith("```"):
            opening, newline, body = content.partition("\n")
            if not newline or "```" in opening[3:] or not body.rstrip().endswith("```"):
                raise ValueError("invalid_code_fence")
            content = body.rstrip()[:-3].strip()
        return content

    async def stage_request(self, operation, messages):
        output = self.settings.llm_output_tokens["chapters" if operation == "concise" else operation]
        try:
            return await self.request(messages, max_tokens=output,
                                      **({"json_mode": False} if operation == "chapters" else {}))
        except AppError as exc:
            if exc.code == "model_output_truncated":
                labels = {"outline": "片段大纲",
                          "extract": "章节信息提取", "chapters": "章节正文", "overview": "整体概览", "issue_quotes": "存疑引文", "concise": "简洁笔记"}
                raise AppError(exc.code, f"{labels[operation]}生成被接口截断（本次输出预算 "
                               f"{output} Token）。已保留成功检查点。", exc.status) from exc
            raise

    def validation_diagnostic(self, operation, payload, exc, *, repair=True):
        reference = None
        if self.diagnostics:
            reference = self.diagnostics.record("model_output_validation", "模型结构有误，将仅修正未完成部分。", exc,
                                    operation=operation, model=self.settings.llm_model,
                                    chapter_ids=[item["chapter_id"] for item in payload.get("chapters", [])])
        self.notify("repair" if repair else "validating", level="warning",
                    message="输出校验未通过，正在修正未完成部分。" if repair else "已忽略不属于本次请求的章节。", error=reference)

    # 逐章校验并等待保存确认，修正时仅请求缺失或无效章节。
    async def generate_chapters(self, payload, validate: ChapterCallbacks):
        if not isinstance(validate, ChapterCallbacks):
            raise TypeError("chapters requires ChapterCallbacks")
        accepted, problems = {}, []
        expected = [item["chapter_id"] for item in payload["chapters"]]
        for repair in range(2):
            self.check_stop()
            if activity_context.get() is not None:
                activity_context.get()["repair"] = repair
            remaining = [item for item in payload["chapters"] if item["chapter_id"] not in accepted]
            current = {**payload, "chapters": remaining}
            messages = self.messages("chapters", current)
            if repair:
                messages.append({"role": "user", "content": "只重写本次输入中尚未完成的章节，保持chapter/summary标记。错误：" +
                                 json.dumps(problems, ensure_ascii=False)})
                if message_characters(messages) > self.settings.llm_input_text_limit:
                    raise AppError("model_input_budget", "章节修正请求超过输入预算，已保存成功章节。")
            content = await self.stage_request("chapters", messages)
            problems = []
            acknowledgements = []
            try:
                drafts = parse_chapters(self.unwrap(content))["chapters"]
                ids = [item["chapter_id"] for item in drafts]
                for request in remaining:
                    identifier = request["chapter_id"]
                    try:
                        if ids.count(identifier) != 1:
                            raise ValueError("chapter_missing_or_duplicate")
                        draft = drafts[ids.index(identifier)]
                        draft = validate.validate_chapter(draft)
                        accepted[identifier] = draft
                        acknowledgements.append(validate.accept(draft))
                    except (ValidationError, ValueError) as exc:
                        problems.append({"chapter_id": identifier, "error": str(exc)})
                if acknowledgements:
                    await asyncio.gather(*acknowledgements)
                if any(i not in expected for i in ids):
                    self.validation_diagnostic("chapters", current, ValueError("unexpected_chapter_discarded"), repair=False)
                if len(accepted) == len(expected):
                    value = {"chapters": [accepted[i] for i in expected]}
                    return validate(value) or value
            except (ValidationError, ValueError) as exc:
                problems.append({"error": str(exc)})
            if repair:
                raise AppError("invalid_model_output", "未完成章节修正一次后仍无效；成功章节已保存。")
            self.validation_diagnostic("chapters", current, ValueError(json.dumps(problems, ensure_ascii=False)))
