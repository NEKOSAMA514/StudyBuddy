from .urls import video_url
from copy import deepcopy
import json
import math
from pathlib import Path
from yt_dlp import YoutubeDL
from ..errors import AppError
from .process import downloader_args, format_selector, network_error_hint, run_process
from .bilibili import is_bilibili, read_parts


def quality_option(info, height):
    # 复用下载器自己的选流与容器判断；只处理已有元数据，不再次访问视频链接。
    source = deepcopy(info)
    source.pop("requested_formats", None)
    source.pop("requested_downloads", None)
    with YoutubeDL({"quiet": True, "no_warnings": True, "check_formats": False,
                    "format": format_selector(), "format_sort": [f"res:{height}", "hasaud"], "format_sort_force": True}) as downloader:
        selected = downloader.process_ie_result(source, download=False)
    parts = selected.get("requested_formats") or [next(
        item for item in selected["formats"] if item["format_id"] == selected["format_id"])]
    sizes, approximate = [], len(parts) > 1
    for part in parts:
        size = part.get("filesize")
        if not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
            size = part.get("filesize_approx")
            approximate = True
        if not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
            return {"height": height, "ext": selected.get("ext"), "filesize": None, "approximate": False}
        sizes.append(size)
    return {"height": height, "ext": selected.get("ext"), "filesize": round(sum(sizes)), "approximate": approximate}


# 固定本次代理读取视频元信息、分 P 和画质，保留实际下载可复用的源信息。
def parse_video(url, stop=None, source_path=None, proxy=""):
    url = video_url(url)
    try:
        parts = read_parts(url, stop, proxy) if is_bilibili(url) else None
        if parts:
            url = parts["source"]["url"]
        raw = run_process([*downloader_args(proxy, url), "-J", "--", url], 45, stop=stop)
    except AppError as error:
        enriched = network_error_hint(url, error, proxy)
        if enriched is error:
            raise
        raise enriched from error
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise AppError("video_parse_failed", "下载器没有返回有效的视频信息。") from exc
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise AppError("video_link_invalid", "本轮仅支持单个视频链接，请选择具体视频。")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise AppError("video_link_invalid", "暂不支持直播或尚未开始的视频。")
    heights = sorted({int(item["height"]) for item in info.get("formats", [])
                      if isinstance(item.get("height"), (int, float)) and math.isfinite(item["height"])
                      and item["height"] > 0 and item.get("vcodec") not in {None, "none"}
                      and not item.get("has_drm")})
    if not heights:
        raise AppError("video_parse_failed", "没有找到可下载的视频画质，当前链接可能需要登录或暂不受支持。")
    if parts:
        # 下载直接读取此快照，保证文件名、时长和来源均对应已选择的 P。
        info.update(title=parts["title"], duration=parts["parts"][parts["selected_part"] - 1]["duration"])
        raw = json.dumps(info, ensure_ascii=False)
    if source_path is not None:
        Path(source_path).write_text(raw, encoding="utf-8")
    duration = info.get("duration")
    duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None
    thumbnail = info.get("thumbnail")
    if not isinstance(thumbnail, str) or not thumbnail.startswith(("https://", "http://")):
        thumbnail = None
    return {**({key: parts[key] for key in ("parts", "selected_part", "source")} if parts else {}),
            "url": url, "title": info.get("title") or info.get("id") or "未命名视频",
            "thumbnail": thumbnail, "duration": duration, "qualities": heights,
            "quality_options": [quality_option(info, height) for height in heights],
            "default_quality": next((height for height in heights if height >= 360), max(heights))}
