"""本机视频下载代理设置，不影响识别和模型请求。"""
import json
import os
from dataclasses import replace
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


class NetworkSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool = False
    url: str = Field(default="", max_length=2048)

    @field_validator("url")
    @classmethod
    def proxy_address(cls, value):
        if not value:
            return value
        address = AnyUrl(value)
        if (address.scheme not in {"http", "https", "socks5", "socks5h"} or not address.host
                or address.username is not None or address.password is not None or address.port == 0
                or address.path not in {None, "/"} or address.query is not None or address.fragment is not None
                or any(ord(char) < 33 for char in value)):
            raise ValueError("请输入无认证的 HTTP、HTTPS、SOCKS5 或 SOCKS5H 代理地址，检查主机和端口。")
        return value

    @model_validator(mode="after")
    def enabled_address(self):
        if self.enabled and not self.url:
            raise ValueError("启用代理时必须填写代理地址。")
        return self


def public_network_settings(settings):
    return {"enabled": settings.video_proxy_enabled, "url": settings.video_proxy_url}


def network_candidate(settings, value):
    return replace(settings, video_proxy_enabled=value.enabled, video_proxy_url=value.url)


# 仅在代理已启用时返回地址，下载操作据此固定本次网络快照。
def effective_proxy(settings):
    return settings.video_proxy_url if settings.video_proxy_enabled else ""


def load_network_settings(settings):
    path = settings.data_dir / "_private" / "network-settings.json"
    if path.exists():
        return network_candidate(settings, NetworkSettingsInput.model_validate_json(path.read_text(encoding="utf-8")))
    return settings


# 代理不含凭据，继续使用普通 JSON 临时写入并替换目标文件。
def save_network_settings(settings):
    path = settings.data_dir / "_private" / "network-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(public_network_settings(settings), file, ensure_ascii=False)
    temporary.replace(path)
