"""MinerU 凭据只保存在私人设置，不进入文档材料或模型快照。"""
from dataclasses import replace
from pydantic import BaseModel, ConfigDict, Field
from .model_store import write_settings
from ..util.private_storage import read_private_json


class MinerUInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(default="", max_length=4096)
    clear_token: bool = False


def public(settings):
    return {"has_token": bool(settings.mineru_token)}


# 留空保留已有 Token，只有显式清除才删除已保存凭据。
def candidate(settings, value):
    return replace(settings, mineru_token="" if value.clear_token else value.token.strip() or settings.mineru_token)


def load(settings):
    path = settings.data_dir / "_private" / "mineru.json"
    if path.exists():
        value = MinerUInput.model_validate(read_private_json(path))
        return candidate(settings, value)
    return settings


# 将 MinerU 凭据整体加密保存，清除状态同样落盘。
def save(settings):
    path = settings.data_dir / "_private" / "mineru.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_settings(path, {"token": settings.mineru_token, "clear_token": not bool(settings.mineru_token)})
