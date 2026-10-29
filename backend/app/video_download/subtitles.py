"""平台字幕读取；主进程管理取消，子进程只下载字幕。"""
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

FORMATS = ("json3", "vtt", "srv3", "srv2", "srv1", "ttml", "srt")
MAX_BYTES = 10 * 1024 * 1024
SUBTITLE_KINDS = {"youtube_subtitle", "bilibili_subtitle"}


def youtube_source(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        identifier = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        identifier = (parse_qs(parsed.query).get("v") or [""])[0] if parts[0] == "watch" else (
            parts[1] if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"} else "")
    else:
        return None
    if re.fullmatch(r"[\w-]{11}", identifier, flags=re.ASCII):
        return {"platform": "youtube", "video_id": identifier, "url": f"https://www.youtube.com/watch?v={identifier}"}
    return None


def content_digest(speech):
    return hashlib.sha256(json.dumps(speech, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def subtitle_cache(folder, source):
    digest = source["digest"]
    if not re.fullmatch(r"[a-f0-9]{16}", digest):
        raise ValueError("invalid_subtitle_digest")
    return folder / f"subtitles-{digest}.json"


def _time(value, rate=30, ticks=1):
    value = value.strip().replace(",", ".")
    match = re.fullmatch(r"([\d.]+)(ms|s|m|h|f|t)", value)
    if match:
        number, unit = match.groups()
        return float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600, "f": 1 / rate, "t": 1 / ticks}[unit]
    parts = value.split(":")
    if len(parts) == 4:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) + float(parts[3]) / rate
    return sum(float(part) * 60 ** i for i, part in enumerate(reversed(parts)))


def _text(value):
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</?(?:b|i|u|s|strike|strong|em|span|font|small|big|sub|sup|c|v|lang|ruby|rt)(?:[.\s][^>]*)?>|<\d{2}:\d{2}[^>]*>",
                   "", value, flags=re.I)
    return html.unescape(value).strip()


def _xml_text(element):
    return (element.text or "") + "".join(
        ("\n" if child.tag.rsplit("}", 1)[-1] == "br" else _xml_text(child)) + (child.tail or "")
        for child in element)


# 将不同字幕格式转换为统一时间片段，并限制时间和正文有效范围。
def parse_subtitle(raw, extension, duration):
    cues = []
    if extension == "bcc":
        for item in json.loads(raw).get("body", []):
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            try:
                cues.append((float(item["from"]), float(item["to"]), item["content"]))
            except (KeyError, TypeError, ValueError):
                continue
    elif extension == "json3":
        for event in json.loads(raw).get("events", []):
            if "tStartMs" in event and "dDurationMs" in event:
                start = float(event["tStartMs"]) / 1000
                cues.append((start, start + float(event["dDurationMs"]) / 1000,
                             "".join(segment.get("utf8", "") for segment in event.get("segs", []))))
    elif extension in {"srt", "vtt"}:
        for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
            if re.match(r"^(NOTE|STYLE|REGION)(?:\s|$)", block.strip()):
                continue
            match = re.search(r"(?m)^([\d:.,]+)\s+-->\s+([\d:.,]+)[^\n]*\n([\s\S]*)", block)
            if match:
                cues.append((_time(match[1]), _time(match[2]), match[3]))
    elif extension in {"srv1", "srv2", "srv3", "ttml"}:
        if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise ValueError("subtitle_xml_entities")
        root = ET.fromstring(raw)
        attrs = {key.rsplit("}", 1)[-1]: value for key, value in root.attrib.items()}
        rate, ticks = float(attrs.get("frameRate", 30)), float(attrs.get("tickRate", 1))
        multiplier = attrs.get("frameRateMultiplier", "1 1").split()
        rate *= float(multiplier[0]) / float(multiplier[1])
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            a = element.attrib
            if extension == "ttml" and tag == "p" and "begin" in a:
                start = _time(a["begin"], rate, ticks)
                end = _time(a["end"], rate, ticks) if "end" in a else start + _time(a["dur"], rate, ticks)
            elif extension != "ttml" and tag in {"text", "p"} and ("start" in a or "t" in a):
                start = float(a["start"]) if "start" in a else float(a["t"]) / 1000
                end = start + (float(a.get("dur", 0)) if "start" in a else float(a.get("d", 0)) / 1000)
            else:
                continue
            cues.append((start, end, _xml_text(element)))
    else:
        raise ValueError("subtitle_format_unsupported")
    result = []
    for start, end, text in sorted(cues, key=lambda item: item[0]):
        text = _text(text)
        if not text or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or start >= duration:
            continue
        end = min(end, duration)
        previous = result[-1] if result else None
        if previous and start < previous["end_seconds"]:
            if previous["text"] == text or text.startswith(previous["text"]):
                previous.update(text=text, end_seconds=max(end, previous["end_seconds"]))
                continue
            old, new = previous["text"].splitlines(), text.splitlines()
            for size in range(min(len(old), len(new)), 0, -1):
                if old[-size:] == new[:size]:
                    text = "\n".join(new[size:])
                    break
        if text:
            result.append({"start_seconds": start, "end_seconds": end, "text": text})
    return result


def _language(value):
    return value.lower().replace("_", "-").removesuffix("-orig").split("-")[0]


# 按目标语言与人工优先规则选择可读取的字幕轨道。
def subtitle_candidates(info, language):
    original = info.get("language")
    if not original:
        original = next((lang for lang in info.get("automatic_captions", {}) if lang.endswith("-orig")), None)
    preferred = original or (language if language != "auto" else None)
    candidates = []
    for automatic, key in ((False, "subtitles"), (True, "automatic_captions")):
        for lang, formats in (info.get(key) or {}).items():
            if lang == "live_chat" or preferred and _language(lang) != _language(preferred):
                continue
            usable = [track for track in formats if track.get("ext") in FORMATS
                      and not any(track.get(key) for key in ("is_partial", "is_forced", "forced"))
                      and track.get("kind") not in {"forced", "partial"}
                      and not parse_qs(urlsplit(track.get("url", "")).query).get("tlang")]
            if usable:
                candidates.append((lang, automatic, sorted(usable, key=lambda track: FORMATS.index(track["ext"]))))
    return candidates


def _download(url, language, duration, proxy, folder):
    from yt_dlp import YoutubeDL
    class Quiet:
        def debug(self, message): pass
        def warning(self, message): pass
        def error(self, message): pass
    options = {"quiet": True, "noprogress": True, "logger": Quiet(), "cachedir": False, "skip_download": True,
               "noplaylist": True, "proxy": proxy, "socket_timeout": 10, "retries": 0,
               "fragment_retries": 0, "skip_unavailable_fragments": False, "extractor_retries": 0, "ignore_no_formats_error": True,
               "max_filesize": MAX_BYTES, "js_runtimes": {"node": {}}}
    failures = []
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        for language, automatic, formats in subtitle_candidates(info, language):
            for track in formats:
                path = folder / f"track-{len(failures)}.{track['ext']}"
                try:
                    request = {**track, "http_headers": track.get("http_headers") or info.get("http_headers") or {}}
                    if "data" in track:
                        path.write_text(track["data"], encoding="utf-8")
                    else:
                        if downloader.dl(str(path), request, subtitle=True)[0] is False:
                            raise ValueError("subtitle_download_failed")
                    if path.stat().st_size > MAX_BYTES:
                        raise ValueError("subtitle_size_limit")
                    speech = parse_subtitle(path.read_text(encoding="utf-8-sig"), track["ext"], duration)
                    if not speech:
                        raise ValueError("subtitle_empty")
                    return {"speech": speech, "source": {"kind": "youtube_subtitle", "language": language,
                            "automatic": automatic, "digest": content_digest(speech)}}
                except Exception as exc:
                    failures.append({"format": track["ext"], "error": type(exc).__name__})
                finally:
                    path.unlink(missing_ok=True)
    return {"speech": [], "reason": "unreadable" if failures else "unavailable", "failures": failures}


def _download_bilibili(source, language, duration, proxy, cookies):
    # 脚本入口不依赖 app 包路径；同目录模块只包含网络协议，不含本机凭据。
    try:
        from .bilibili_client import BilibiliClient
    except ImportError:
        from bilibili_client import BilibiliClient
    client = BilibiliClient(proxy)
    tracks, reason = client.subtitles(source, language, cookies)
    failures = []
    for track in tracks:
        try:
            payload = client.request(track["url"])[0]
            speech = parse_subtitle(json.dumps(payload), "bcc", duration)
            if not speech:
                raise ValueError("subtitle_empty")
            return {"speech": speech, "source": {"kind": "bilibili_subtitle", "language": track["language"],
                    "automatic": track["automatic"], "track_id": track["track_id"], "digest": content_digest(speech)}}
        except Exception:
            failures.append({"error": "subtitle_unreadable"})
    return {"speech": [], "reason": "unreadable" if failures else reason, "failures": failures}


# 按已确认平台尝试读取字幕并固定来源摘要，不可用时返回明确回退原因。
def fetch_subtitles(source, language, duration, folder, proxy="", stop=None, cookies=None):
    from .process import run_process
    with tempfile.TemporaryDirectory(prefix="subtitle-read-", dir=folder) as temporary:
        args = [sys.executable, str(Path(__file__).resolve()), source["url"], language,
                str(duration), proxy, temporary]
        if source.get("platform") == "bilibili":
            raw = run_process([*args, "bilibili"], 90, stop=stop,
                              input_text=json.dumps({"source": source, "cookies": cookies or {}}))
        else:
            raw = run_process(args, 90, stop=stop)
        return json.loads(raw)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 6 and sys.argv[6] == "bilibili":
            private = json.load(sys.stdin)
            result = _download_bilibili(private["source"], sys.argv[2], float(sys.argv[3]), sys.argv[4], private["cookies"])
        else:
            result = _download(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], Path(sys.argv[5]))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # 不把字幕签名 URL 或提取器响应输出到日志。
        print(json.dumps({"speech": [], "reason": getattr(exc, "reason", "fetch_failed")}))
