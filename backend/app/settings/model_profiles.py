"""多组连接共用一个原子保存文件；公开数据不包含密钥。"""
import uuid
from dataclasses import replace
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..errors import AppError
from .model_settings import ModelSettingsInput, candidate, public_settings
from .model_defaults import MODELSCOPE_BASE_URL
from ..analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT, OUTPUT_TOKENS, task_budgets, restore_task_budgets

EXECUTION_FIELDS = ("llm_base_url", "llm_stream", "llm_json_mode", "summary_prompt", "note_style",
                    "llm_section_concurrency", "llm_request_interval_seconds")


# 只保存可恢复的执行字段和连接编号，密钥不进入任务快照。
def task_model_config(settings):
    return {"model": settings.llm_model, "provider": settings.llm_provider,
            "llm_profile_id": settings.llm_profile_id or "default",
            **{key: getattr(settings, key) for key in EXECUTION_FIELDS}, **task_budgets(settings)}


# 先找回任务原连接的当前密钥，再还原任务保存的执行参数与预算。
def execution_settings(current, config, *, required_fields=()):
    settings = execution_profile(current, config)
    fields = {key: config[key] for key in EXECUTION_FIELDS if key in required_fields or key in config}
    return restore_task_budgets(replace(settings, **fields, llm_model=config["model"], llm_provider=config["provider"]), config)


class ProfileInput(ModelSettingsInput):
    name: str = Field(default="自定义 API", min_length=1, max_length=80)


class ModelOptionsInput(ProfileInput):
    name: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=200)


class StoredProfile(ProfileInput):
    id: str = Field(min_length=1, max_length=64)


class ProfileStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2] = 2
    active_id: str
    profiles: list[StoredProfile] = Field(min_length=1, max_length=50)
    summary_prompt: str = Field(default="", max_length=10000)
    note_style: Literal["concise", "detailed"] = "detailed"

    @model_validator(mode="after")
    def valid_ids(self):
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)) or self.active_id not in ids:
            raise ValueError("连接编号重复或当前连接不存在")
        return self


def record(settings, identifier, name):
    values = public_settings(settings)
    for key in ("provider", "has_token", "summary_prompt", "default_summary_prompt", "note_style"):
        values.pop(key)
    return {"id": identifier, "name": name, **values, "token": settings.llm_key,
            "clear_token": not bool(settings.llm_key)}


def profiles(settings):
    if not settings.llm_profiles:
        return (record(settings, "default", "魔搭" if settings.llm_base_url == MODELSCOPE_BASE_URL else "默认连接"),)
    return tuple(record(settings, p["id"], p["name"]) if p["id"] == settings.llm_profile_id else p
                 for p in settings.llm_profiles)


def active_id(settings):
    return settings.llm_profile_id or "default"


def find(settings, identifier):
    for profile in profiles(settings):
        if profile["id"] == identifier:
            return profile
    raise AppError("invalid_input", "这组 API 配置已不存在，请刷新设置。", 404)


def for_profile(settings, profile):
    value = ModelSettingsInput.model_validate({k: v for k, v in profile.items() if k not in {"id", "name"}})
    # 存储的密钥（包括空密钥）优先，绝不回退到其他连接或环境变量。
    base = replace(settings, llm_key="",
                   llm_input_text_limit=DEFAULT_INPUT_TEXT_LIMIT, llm_output_tokens=dict(OUTPUT_TOKENS))
    return replace(candidate(base, value), llm_profile_id=profile["id"],
                   summary_prompt=settings.summary_prompt, note_style=settings.note_style)


def preview(settings, value, identifier=None):
    base = for_profile(settings, find(settings, identifier)) if identifier else replace(settings, llm_key="")
    return candidate(base, value)


# 生成配置管理响应，只暴露是否已有密钥，不返回密钥内容。
def public_profiles(settings):
    result = []
    for profile in profiles(settings):
        public = public_settings(for_profile(settings, profile))
        public.pop("summary_prompt")
        public.pop("default_summary_prompt")
        public.pop("note_style")
        result.append({"id": profile["id"], "name": profile["name"], **public})
    return {"active_id": active_id(settings), "profiles": result}


# 计算新增或编辑连接后的候选快照，是否保存与发布由设置服务决定。
def edit(settings, value, identifier=None):
    saved = profiles(settings)
    if identifier is None and len(saved) >= 50:
        raise AppError("invalid_input", "最多保存 50 组 API 配置。", 422)
    updated = preview(settings, value, identifier)
    identifier = identifier or uuid.uuid4().hex
    entry = record(updated, identifier, value.name)
    values = tuple(entry if p["id"] == identifier else p for p in saved)
    if not any(p["id"] == identifier for p in saved):
        values += (entry,)
    updated = replace(settings, llm_profiles=values, llm_profile_id=active_id(settings))
    return for_profile(updated, entry) if identifier == active_id(settings) else updated


def activate(settings, identifier):
    entry = find(settings, identifier)
    return for_profile(replace(settings, llm_profiles=profiles(settings)), entry)


def remove(settings, identifier):
    find(settings, identifier)
    if identifier == active_id(settings):
        raise AppError("invalid_input", "请先启用另一组配置，再删除当前配置。", 409)
    return replace(settings, llm_profile_id=active_id(settings),
                   llm_profiles=tuple(p for p in profiles(settings) if p["id"] != identifier))


def stored_profiles(settings):
    return {"version": 2, "active_id": active_id(settings), "profiles": profiles(settings),
            "summary_prompt": settings.summary_prompt, "note_style": settings.note_style}


def load_profiles(settings, raw):
    store = ProfileStore.model_validate(raw)
    updated = replace(settings, llm_profile_id="", llm_profiles=tuple(p.model_dump(exclude={"summary_prompt"}) for p in store.profiles),
                      summary_prompt=store.summary_prompt, note_style=store.note_style)
    return activate(updated, store.active_id)


# 按连接编号及原地址匹配凭据，禁止把新服务商的密钥用于旧任务地址。
def execution_profile(settings, config):
    identifier = config.get("llm_profile_id")
    if identifier:
        entry = find(settings, identifier)
        if entry["base_url"] != config["llm_base_url"]:
            raise AppError("model_not_configured", "任务所用 API 地址已修改，请恢复原连接地址或重新生成笔记。")
        return for_profile(settings, entry)
    # 兼容没有连接编号的历史任务，只能复用地址匹配的密钥。
    endpoint = config.get("llm_base_url", settings.llm_base_url)
    if endpoint == settings.llm_base_url:
        return settings
    matches = [p for p in profiles(settings) if p["base_url"] == endpoint]
    if len(matches) != 1:
        raise AppError("model_not_configured", "请恢复该历史任务使用的 API 配置，或重新生成笔记。")
    return for_profile(settings, matches[0])
