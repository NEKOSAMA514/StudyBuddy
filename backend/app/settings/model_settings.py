"""仅由后端读取的本机模型设置；公开响应永不包含密钥。"""
from dataclasses import replace
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .model_defaults import DEFAULT_SUMMARY_PROMPT
from .model_defaults import MODELSCOPE_PROVIDER, MODELSCOPE_BASE_URL, DEFAULT_LLM_MODEL
from ..analysis.budgets import OUTPUT_TOKENS


class OutputTokensInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outline: int = Field(default=OUTPUT_TOKENS["outline"], ge=1, strict=True)
    extract: int = Field(default=OUTPUT_TOKENS["extract"], ge=1, strict=True)
    chapters: int = Field(default=OUTPUT_TOKENS["chapters"], ge=1, strict=True)
    overview: int = Field(default=OUTPUT_TOKENS["overview"], ge=1, strict=True)
    issue_quotes: int = Field(default=OUTPUT_TOKENS["issue_quotes"], ge=1, strict=True)
    connection_test: int = Field(default=OUTPUT_TOKENS["connection_test"], ge=1, strict=True)


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(default=MODELSCOPE_BASE_URL, max_length=2048)
    model: str = Field(default=DEFAULT_LLM_MODEL, min_length=1, max_length=200)
    token: str = Field(default="", max_length=4096, repr=False)
    clear_token: bool = False
    stream: bool = True
    json_mode: bool = False
    free_tier_confirmed: bool = False  # 兼容旧设置文件，不再作为调用前置条件。
    section_concurrency: int | None = Field(default=None, ge=1, le=4, strict=True)
    request_interval_seconds: int | None = Field(default=None, ge=1, le=60, strict=True)
    input_text_limit: int | None = Field(default=None, ge=1, strict=True)
    output_tokens: OutputTokensInput | None = None
    summary_prompt: str | None = Field(default=None, max_length=10000)
    note_style: Literal["concise", "detailed"] | None = None

    @field_validator("base_url")
    @classmethod
    def trusted_endpoint(cls, value):
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in value)):
            raise ValueError("请填写不含认证信息、查询参数或片段的 HTTP(S) API 地址")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 API 必须使用 HTTPS；本机接口可使用 HTTP")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("API 端口无效")
        return value.rstrip("/")


class NoteStyleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary_prompt: str = Field(max_length=10000)
    note_style: Literal["concise", "detailed"] = "detailed"


# 按留空保留、显式清除和地址变更规则计算候选设置，不直接写盘。
def candidate(settings, value):
    providers = {MODELSCOPE_BASE_URL: MODELSCOPE_PROVIDER,
                 "https://dashscope.aliyuncs.com/compatible-mode/v1": "bailian"}
    return replace(settings, llm_provider=providers.get(value.base_url, "openai-compatible"), llm_base_url=value.base_url,
                   llm_model=value.model, llm_key="" if value.clear_token else value.token or (settings.llm_key if value.base_url == settings.llm_base_url else ""),
                   llm_stream=value.stream, llm_json_mode=value.json_mode,
                   free_tier_confirmed=value.free_tier_confirmed,
                   llm_section_concurrency=value.section_concurrency if value.section_concurrency is not None else settings.llm_section_concurrency,
                   llm_request_interval_seconds=value.request_interval_seconds if value.request_interval_seconds is not None else settings.llm_request_interval_seconds,
                   llm_input_text_limit=value.input_text_limit if value.input_text_limit is not None else settings.llm_input_text_limit,
                   llm_output_tokens=value.output_tokens.model_dump() if value.output_tokens is not None else dict(settings.llm_output_tokens),
                   summary_prompt=value.summary_prompt if value.summary_prompt is not None else settings.summary_prompt,
                   note_style=value.note_style if value.note_style is not None else settings.note_style)


# 构造前端可见配置，密钥只转换为是否已保存的布尔值。
def public_settings(settings):
    return {"provider": settings.llm_provider, "base_url": settings.llm_base_url,
            "model": settings.llm_model, "has_token": bool(settings.llm_key),
            "stream": settings.llm_stream, "json_mode": settings.llm_json_mode,
            "free_tier_confirmed": settings.free_tier_confirmed,
            "section_concurrency": settings.llm_section_concurrency,
            "request_interval_seconds": settings.llm_request_interval_seconds,
            "input_text_limit": settings.llm_input_text_limit, "note_style": settings.note_style,
            "output_tokens": dict(settings.llm_output_tokens),
            "summary_prompt": settings.summary_prompt, "default_summary_prompt": DEFAULT_SUMMARY_PROMPT}
