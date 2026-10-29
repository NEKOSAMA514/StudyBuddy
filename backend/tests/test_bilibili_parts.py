import io
import json
import threading
import pytest
from app.errors import AppError
from app.video_download import bilibili, metadata


@pytest.fixture
def bili_info():
    return {"bvid": "BV1UhpK6RErH", "title": "测试课程", "pages": [
        {"page": 1, "cid": 123, "part": "第一讲", "duration": 60},
        {"page": 2, "cid": 456, "part": "第二讲", "duration": 120}]}


@pytest.mark.parametrize("query,page", [("", 1), ("?p=2", 2), ("?spm_id_from=x&p=2&vd_source=y", 2)])
def test_part_selection_and_tracking(bili_info, query, page):
    identity, selected = bilibili.video_identity("https://www.bilibili.com/video/BV1UhpK6RErH/" + query)
    assert identity == bili_info["bvid"] and selected == page
    result = bilibili.parts_from_info(bili_info, selected)
    assert result["selected_part"] == page and len(result["parts"]) == 2
    assert result["source"] == {"platform": "bilibili", "video_id": identity, "page": page,
                                "cid": [123, 456][page - 1], "url": f"https://www.bilibili.com/video/{identity}?p={page}"}
    assert result["title"].startswith(f"P{page} ")


@pytest.mark.parametrize("query", ["?p=0", "?p=-1", "?p=abc", "?p=", "?p=1&p=2", "?p=1.5"])
def test_invalid_part_is_never_defaulted(query):
    with pytest.raises(ValueError, match="分 P 编号无效"):
        bilibili.video_identity("https://www.bilibili.com/video/BV1UhpK6RErH" + query)


def test_single_part_and_missing_part(bili_info):
    bili_info["pages"] = bili_info["pages"][:1]
    result = bilibili.parts_from_info(bili_info, 1)
    assert result["title"] == "测试课程" and len(result["parts"]) == 1
    with pytest.raises(ValueError, match="不存在"):
        bilibili.parts_from_info(bili_info, 2)
    bili_info["pages"] = []
    with pytest.raises(ValueError, match="清单"):
        bilibili.parts_from_info(bili_info, 1)


@pytest.mark.parametrize("pages", [None, [{"page": 1}], [{"page": 2, "cid": 123, "duration": 60}]])
def test_incomplete_list_cannot_masquerade_as_single_part(bili_info, pages):
    bili_info["pages"] = pages
    with pytest.raises(ValueError, match="清单"):
        bilibili.parts_from_info(bili_info, 1)


def test_short_link_resolves_before_part_and_av_uses_same_bvid(monkeypatch, bili_info):
    import yt_dlp
    requests, options = [], []

    class Downloader:
        def __init__(self, config):
            options.append(config)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def urlopen(self, request):
            requests.append(request.url)
            response = io.BytesIO(json.dumps({"code": 0, "data": bili_info}).encode())
            response.url = "https://www.bilibili.com/video/av123?p=2&spm_id_from=tracking"
            return response

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Downloader)
    result = bilibili._read("https://b23.tv/test", "http://127.0.0.1:7890")
    assert requests == ["https://b23.tv/test", "https://api.bilibili.com/x/web-interface/view?aid=123"]
    assert result["source"]["cid"] == 456 and result["source"]["page"] == 2
    assert options[0]["proxy"] == "http://127.0.0.1:7890" and options[0]["socket_timeout"] == 10


def test_part_reader_uses_cancelable_process_and_reports_errors(monkeypatch):
    from app.video_download import process
    stopped = threading.Event()
    calls = []
    def run(args, timeout, stop):
        calls.append((args, timeout, stop))
        return '{"error":"指定的分 P 不存在"}'
    monkeypatch.setattr(process, "run_process", run)
    with pytest.raises(AppError, match="不存在"):
        bilibili.read_parts("https://b23.tv/test", stopped, "http://proxy.test")
    assert calls[0][0][-2:] == ["https://b23.tv/test", "http://proxy.test"]
    assert calls[0][1:] == (45, stopped)


@pytest.mark.parametrize("shared", ["https://b23.tv/test", "【视频分享】 https://b23.tv/test",
    "【视频分享】 https://www.bilibili.com/video/BV1UhpK6RErH?p=2&share_source=copy_web"])
def test_selected_part_drives_snapshot_quality_and_source(monkeypatch, bili_info, tmp_path, shared):
    calls = []
    def read_parts(url, *args):
        assert url == shared.removeprefix("【视频分享】 ")
        return bilibili.parts_from_info(bili_info, 2)
    monkeypatch.setattr(metadata, "read_parts", read_parts)
    stream = {"id": "test", "title": "错误的总标题", "duration": 999, "formats": [
        {"format_id": "360", "url": "https://example.com/v.mp4", "height": 360,
         "ext": "mp4", "vcodec": "h264", "acodec": "aac", "filesize": 1024}]}
    monkeypatch.setattr(metadata, "run_process", lambda args, *a, **k: calls.append(args) or json.dumps(stream))
    snapshot = tmp_path / "source.info.json"
    result = metadata.parse_video(shared, source_path=snapshot)
    assert calls[0][-1] == "https://www.bilibili.com/video/BV1UhpK6RErH?p=2"
    assert result["url"] == calls[0][-1] and result["duration"] == 120
    assert result["default_quality"] == 360 and result["source"]["cid"] == 456
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["title"] == result["title"] and saved["title"].startswith("P2 第二讲")
    assert saved["duration"] == 120


def test_list_failure_does_not_start_stream_extraction(monkeypatch):
    def fail(*args):
        raise AppError("video_parse_failed", "分 P 清单读取失败")
    monkeypatch.setattr(metadata, "read_parts", fail)
    monkeypatch.setattr(metadata, "run_process", lambda *a, **k: pytest.fail("不得下载 P1"))
    with pytest.raises(AppError, match="清单读取失败"):
        metadata.parse_video("https://www.bilibili.com/video/BV1UhpK6RErH?p=2")
