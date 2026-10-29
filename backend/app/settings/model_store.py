"""模型配置的原子读写；兼容单组和多组文件。"""
from copy import deepcopy
from datetime import datetime
from .model_settings import ModelSettingsInput, candidate, public_settings
from .model_profiles import load_profiles, stored_profiles, ProfileStore
from ..util.private_storage import read_private_json, write_private_json, write_private_bytes, decode_private


def settings_path(settings):
    return settings.data_dir / "_private" / "model-settings.json"


# 解密并校验模型配置，兼容旧单组文件及多组连接格式。
def load_settings(settings):
    path = settings_path(settings)
    if path.exists():
        raw = read_private_json(path)
        raw = consolidate_input_limit(path, raw)
        if "profiles" in raw:
            return load_profiles(settings, raw)
        value = ModelSettingsInput.model_validate(raw)
        return candidate(settings, value)
    return settings


# 升级旧预算字段前先校验并加密备份原始内容，失败时不发布新设置。
def consolidate_input_limit(path, raw):
    records = raw.get("profiles", [raw])
    if not any("outline_text_limit" in record for record in records):
        return raw
    updated = validate_stored(raw)
    backup = path.with_name(path.name + ".before-input-limit-" + datetime.now().strftime("%Y%m%d%H%M%S%f") + ".bak")
    payload, _ = decode_private(path.read_bytes())
    write_private_bytes(backup, payload)
    write_settings(path, updated)
    return updated


def validate_stored(raw):
    updated = deepcopy(raw)
    for record in updated.get("profiles", [updated]):
        record.pop("outline_text_limit", None)
    (ProfileStore if "profiles" in updated else ModelSettingsInput).model_validate(updated)
    return updated


# 完整保存多组连接及空密钥状态，避免重启后恢复已删除的环境密钥。
def save_settings(settings):
    path = settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if settings.llm_profiles:
        value = stored_profiles(settings)
    else:
        value = public_settings(settings)
        value.pop("provider")
        value.pop("has_token")
        value.pop("default_summary_prompt")
        value["token"] = settings.llm_key
        # 空密钥也必须覆盖环境中的旧值，删除后重启不能恢复旧密钥。
        value["clear_token"] = not bool(settings.llm_key)
    write_settings(path, value)


def write_settings(path, value):
    write_private_json(path, value)
