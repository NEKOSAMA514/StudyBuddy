"""B 站登录和字幕的有限网络读取，不记录响应头或签名地址。"""
import hashlib
from http.cookiejar import Cookie
import json
from pathlib import PurePosixPath
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

MAX_BYTES = 10 * 1024 * 1024
COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
MIXIN_ORDER = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
               27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
               37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
               22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)


class BilibiliError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# 从导航返回的密钥素材构造 WBI 签名，不将签名地址写入诊断日志。
def sign_wbi(params, navigation, now=None):
    images = (navigation.get("data") or {}).get("wbi_img") or {}
    raw = "".join(PurePosixPath(urlsplit(images.get(key, "")).path).stem for key in ("img_url", "sub_url"))
    if len(raw) != 64:
        raise BilibiliError("wbi_unavailable")
    key = "".join(raw[index] for index in MIXIN_ORDER)[:32]
    values = {name: re.sub(r"[!'()*]", "", str(value)) for name, value in params.items()}
    values["wts"] = str(int(time.time() if now is None else now))
    query = urlencode(sorted(values.items()))
    return {**values, "w_rid": hashlib.md5((query + key).encode()).hexdigest()}


# 封装 B 站登录与字幕所需的有限请求，代理由本次操作快照提供。
class BilibiliClient:
    def __init__(self, proxy=""):
        self.proxy = proxy

    def request(self, url, params=None, cookies=None):
        from yt_dlp import YoutubeDL
        from yt_dlp.networking import Request

        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise BilibiliError("invalid_subtitle_url")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com/",
                   "Origin": "https://www.bilibili.com"}
        if params:
            url += "?" + urlencode(params)
        class Quiet:
            def debug(self, message): pass
            def warning(self, message): pass
            def error(self, message): pass
        try:
            with YoutubeDL({"proxy": self.proxy, "socket_timeout": 10, "cachedir": False,
                            "quiet": True, "logger": Quiet()}) as downloader:
                if cookies and parsed.hostname in {"api.bilibili.com", "passport.bilibili.com"}:
                    for name in COOKIE_NAMES:
                        if cookies.get(name):
                            downloader.cookiejar.set_cookie(Cookie(0, name, cookies[name], None, False,
                                ".bilibili.com", True, True, "/", True, True, None, True, None, None, {}))
                with downloader.urlopen(Request(url, headers=headers)) as response:
                    raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise BilibiliError("response_too_large")
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise BilibiliError("invalid_response")
                    return value, response.headers.get_all("Set-Cookie") or []
        except BilibiliError:
            raise
        except Exception:
            raise BilibiliError("network_failed") from None

    def navigation(self, cookies=None):
        return self.request("https://api.bilibili.com/x/web-interface/nav", cookies=cookies)[0]

    def subtitles(self, source, language, cookies=None):
        nav = self.navigation(cookies)
        if cookies and nav.get("code") == -101:
            return [], "credentials_expired"
        params = sign_wbi({"bvid": source["video_id"], "cid": source["cid"]}, nav)
        payload = self.request("https://api.bilibili.com/x/player/wbi/v2", params, cookies)[0]
        if payload.get("code") != 0:
            raise BilibiliError("player_unavailable")
        data = payload.get("data") or {}
        tracks = (data.get("subtitle") or {}).get("subtitles") or []
        if data.get("need_login_subtitle") and not tracks:
            return [], "credentials_missing" if not cookies else "credentials_expired"
        preferred = language.split("-")[0] if language != "auto" else None
        candidates = []
        for track in tracks:
            lan = track.get("lan", "")
            automatic = lan.startswith("ai-") or track.get("ai_type") not in (None, 0, "0", "")
            normalized = lan.removeprefix("ai-").lower().replace("_", "-")
            if (not track.get("subtitle_url") or preferred and normalized.split("-")[0] != preferred
                    or track.get("is_translated") or track.get("is_auto_translated")
                    or track.get("is_partial") or track.get("is_forced")
                    or track.get("kind") == "translated"
                    or parse_qs(urlsplit(track["subtitle_url"]).query).get("tlang")):
                continue
            url = track["subtitle_url"]
            if url.startswith("//"):
                url = "https:" + url
            host = (urlsplit(url).hostname or "").lower()
            if not (host == "bilibili.com" or host.endswith(".bilibili.com")
                    or host == "hdslb.com" or host.endswith(".hdslb.com")):
                continue
            candidates.append({"url": url, "language": normalized, "automatic": automatic,
                               "track_id": str(track.get("id_str") or track.get("id") or lan)})
        return sorted(candidates, key=lambda item: item["automatic"]), "unavailable"
