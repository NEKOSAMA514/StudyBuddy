import io
import json
from email.message import Message
import pytest
from app.video_download import bilibili_client as client_module, subtitles
from app.video_download.bilibili_client import BilibiliClient, BilibiliError, sign_wbi
from app.video_download.process import run_process

SOURCE = {"platform": "bilibili", "video_id": "BV1UhpK6RErH", "cid": 456, "page": 2,
          "url": "https://www.bilibili.com/video/BV1UhpK6RErH?p=2"}
NAV = {"code": 0, "data": {"isLogin": True, "wbi_img": {
    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}}


def test_wbi_known_signature():
    result = sign_wbi({"foo": "114", "bar": "514", "baz": 1919810}, NAV, now=1702204169)
    # 此固定向量已与当前 yt-dlp 的 WBI 实现交叉核对。
    assert result["w_rid"] == "6149fdadf571698ca7e6a567265cd0ee"
    assert sign_wbi({"foo": "a!b'c(d)*"}, NAV, now=1)["foo"] == "abcd"


def test_bcc_keeps_seconds_text_and_distant_repetitions():
    raw = {"body": [{"from": .1, "to": 1.5, "content": "<b>甲</b> &amp; 乙"},
                    {"from": 3, "to": 4, "content": "甲 & 乙"},
                    {"from": -1, "to": 1, "content": "bad"}, {"from": 0, "to": 1, "content": None}, {}]}
    assert subtitles.parse_subtitle(json.dumps(raw), "bcc", 10) == [
        {"start_seconds": .1, "end_seconds": 1.5, "text": "甲 & 乙"},
        {"start_seconds": 3, "end_seconds": 4, "text": "甲 & 乙"}]


def test_candidates_ordinary_before_ai_and_correct_cid(monkeypatch):
    seen = []
    tracks = [
        {"id": 1, "lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/auto"},
        {"id": 2, "lan": "zh-CN", "ai_type": "0", "subtitle_url": "https://aisubtitle.hdslb.com/manual"},
        {"id": 3, "lan": "zh", "subtitle_url": "https://aisubtitle.hdslb.com/translated", "is_translated": True},
        {"id": 4, "lan": "en", "subtitle_url": "https://aisubtitle.hdslb.com/en"},
        {"id": 5, "lan": "zh", "subtitle_url": "https://foreign.invalid/stolen"}]
    def request(self, url, params=None, cookies=None):
        seen.append((url, params, cookies))
        return (NAV if url.endswith("/nav") else {"code": 0, "data": {"subtitle": {"subtitles": tracks}}}), []
    monkeypatch.setattr(BilibiliClient, "request", request)
    found, _ = BilibiliClient().subtitles(SOURCE, "zh", {"SESSDATA": "test-only"})
    assert [track["track_id"] for track in found] == ["2", "1"]
    assert [track["automatic"] for track in found] == [False, True]
    assert seen[1][1]["cid"] == "456" and seen[1][1]["bvid"] == SOURCE["video_id"]
    assert "w_rid" in seen[1][1]


@pytest.mark.parametrize("cookies,expected", [({}, "credentials_missing"), ({"SESSDATA": "test"}, "credentials_expired")])
def test_login_requirement_does_not_masquerade_as_no_subtitles(monkeypatch, cookies, expected):
    monkeypatch.setattr(BilibiliClient, "navigation", lambda *a: NAV)
    monkeypatch.setattr(BilibiliClient, "request", lambda *a: ({"code": 0, "data": {"need_login_subtitle": True}}, []))
    assert BilibiliClient().subtitles(SOURCE, "zh", cookies) == ([], expected)


def test_failed_track_tries_next_and_cdn_gets_no_cookie(monkeypatch):
    monkeypatch.setattr(BilibiliClient, "subtitles", lambda *a: ([
        {"url": "https://aisubtitle.hdslb.com/bad", "language": "zh", "automatic": False, "track_id": "1"},
        {"url": "https://aisubtitle.hdslb.com/good", "language": "zh", "automatic": True, "track_id": "2"}], "unavailable"))
    def request(self, url, params=None, cookies=None):
        assert cookies is None
        if url.endswith("/bad"):
            raise BilibiliError("network_failed")
        return {"body": [{"from": 0, "to": 1, "content": "本章原文"}]}, []
    monkeypatch.setattr(BilibiliClient, "request", request)
    result = subtitles._download_bilibili(SOURCE, "zh", 2, "", {"SESSDATA": "unit-secret"})
    assert result["source"]["kind"] == "bilibili_subtitle" and result["source"]["automatic"]
    assert result["source"]["track_id"] == "2" and "unit-secret" not in json.dumps(result)


def test_cookies_are_stdin_only_and_process_can_read_them(monkeypatch, tmp_path):
    from app.video_download import process
    calls = []
    def run(args, timeout, stop, input_text):
        calls.append((args, input_text))
        return '{"speech":[],"reason":"unavailable"}'
    monkeypatch.setattr(process, "run_process", run)
    subtitles.fetch_subtitles(SOURCE, "zh", 2, tmp_path, cookies={"SESSDATA": "unit-secret"})
    assert "unit-secret" not in json.dumps(calls[0][0])
    assert json.loads(calls[0][1]) == {"source": SOURCE, "cookies": {"SESSDATA": "unit-secret"}}


def test_process_stdin_transport_does_not_echo_secret():
    import sys
    result = run_process([sys.executable, "-c", "import sys; x=sys.stdin.read(); print(len(x))"], 10, input_text="unit-secret")
    assert result.strip() == "11" and "unit-secret" not in result


def test_network_client_limits_cookie_to_bilibili_and_sanitizes_errors(monkeypatch):
    import yt_dlp
    seen = []
    class Downloader:
        def __init__(self, options):
            from http.cookiejar import CookieJar
            self.cookiejar = CookieJar()
            assert options["proxy"] == "socks5h://127.0.0.1:1080" and options["socket_timeout"] == 10
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def urlopen(self, request):
            seen.append((request, list(self.cookiejar)))
            response = io.BytesIO(b'{"code":0}')
            response.headers = Message()
            return response
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    client = BilibiliClient("socks5h://127.0.0.1:1080")
    client.request("https://api.bilibili.com/test", cookies={"SESSDATA": "unit-secret"})
    client.request("https://aisubtitle.hdslb.com/test", cookies={"SESSDATA": "unit-secret"})
    assert seen[0][1][0].domain == ".bilibili.com" and seen[0][1][0].secure
    assert seen[0][1][0].value == "unit-secret"
    assert not seen[1][1] and not seen[1][0].headers.get("Cookie")


def test_network_exception_suppresses_sensitive_transport_context(monkeypatch):
    import yt_dlp
    class Downloader:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def urlopen(self, request):
            raise RuntimeError("SESSDATA=unit-secret; signed-private-url")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    with pytest.raises(BilibiliError) as error:
        BilibiliClient().request("https://api.bilibili.com/test")
    assert "unit-secret" not in str(error.value) and error.value.__suppress_context__
