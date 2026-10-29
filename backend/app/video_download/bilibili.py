"""读取 B 站分 P 清单；网络请求在可取消的下载子进程内执行。"""
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlencode, urlsplit


def is_bilibili(url):
    return (urlsplit(url).hostname or "").lower() in {
        "bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"}


def video_identity(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parsed.path)
    if not is_bilibili(url) or not match:
        raise ValueError("请使用 B 站普通视频链接。")
    values = parse_qs(parsed.query, keep_blank_values=True).get("p", ["1"])
    if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]*", values[0]):
        raise ValueError("分 P 编号无效，请选择有效的 P。")
    return match[1], int(values[0])


def parts_from_info(info, page):
    bvid = info.get("bvid", "")
    pages = info.get("pages")
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not isinstance(pages, list) or not pages:
        raise ValueError("无法读取分 P 清单，请重新解析链接。")
    parts = []
    for item in pages:
        number, cid, duration = item.get("page"), item.get("cid"), item.get("duration")
        if (type(number) is not int or number != len(parts) + 1 or type(cid) is not int or cid <= 0
                or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
            raise ValueError("分 P 清单不完整，请重新解析链接。")
        parts.append({"page": number, "cid": cid, "title": item.get("part") or f"P{number}",
                      "duration": duration, "url": f"https://www.bilibili.com/video/{bvid}?p={number}"})
    if page > len(parts):
        raise ValueError("指定的分 P 不存在，请检查链接中的 p 参数。")
    selected = parts[page - 1]
    title = info.get("title") or bvid
    if len(parts) > 1:
        title = f'P{page} {selected["title"]} · {title}'
    return {"parts": parts, "selected_part": page, "title": title,
            "source": {"platform": "bilibili", "video_id": bvid, "page": page,
                       "cid": selected["cid"], "url": selected["url"]}}


def _read(url, proxy):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking import Request

    options = {"proxy": proxy, "socket_timeout": 10, "cachedir": False, "quiet": True, "no_warnings": True}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    with YoutubeDL(options) as downloader:
        if (urlsplit(url).hostname or "").lower() in {"b23.tv", "www.b23.tv"}:
            with downloader.urlopen(Request(url, headers=headers)) as response:
                url = response.url
        identifier, page = video_identity(url)
        query = {"bvid": identifier} if identifier.startswith("BV") else {"aid": identifier[2:]}
        endpoint = "https://api.bilibili.com/x/web-interface/view?" + urlencode(query)
        with downloader.urlopen(Request(endpoint, headers=headers)) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise ValueError("B 站视频信息读取失败，请检查链接后重新解析。")
        return parts_from_info(result.get("data") or {}, page)


# 通过子进程读取 B 站分 P 信息，保留本文件作为脚本启动入口的兼容方式。
def read_parts(url, stop=None, proxy=""):
    from ..errors import AppError
    from .process import run_process

    raw = run_process([sys.executable, str(Path(__file__).resolve()), url, proxy], 45, stop=stop)
    try:
        result = json.loads(raw)
        if "error" in result:
            raise AppError("video_link_invalid", result["error"])
        return result
    except (ValueError, TypeError) as exc:
        raise AppError("video_parse_failed", "无法读取 B 站分 P 清单，请重新解析链接。") from exc


if __name__ == "__main__":
    try:
        result = _read(*sys.argv[1:])
    except ValueError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "B 站分 P 清单读取失败，请检查网络后重新解析。"}
    print(json.dumps(result, ensure_ascii=False))
