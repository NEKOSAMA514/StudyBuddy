"""视频链接校验与分享文案规范化。"""
import re
from urllib.parse import urlsplit
from ..errors import AppError
from .bilibili import is_bilibili


# 规范单个链接、BV 号和 B 站分享文案，保留分 P 参数供后续解析。
def video_url(value):
    value = value.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        value = f"https://www.bilibili.com/video/{value}"
    else:
        links = re.findall(r"https?://[^\s<>\"'【】（）()，。！？、；]+", value, flags=re.I)
        if len(links) == 1:
            try:
                if is_bilibili(links[0]):
                    value = links[0].rstrip(".,;!")
            except ValueError:
                pass
    try:
        url = urlsplit(value)
        valid = url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
        if any(ord(char) < 33 for char in value):
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise AppError("video_link_invalid", "请输入单个有效的 HTTP 或 HTTPS 视频链接。")
    return value
