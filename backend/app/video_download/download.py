from .urls import video_url
import json
import math
from pathlib import Path
from ..errors import AppError
from .process import downloader_args, format_selector, network_error_hint, run_process


# 确认下载成品位于任务目录且包含有效视频轨道，不把残留临时文件当成功。
def verify_video(path, directory, stop=None):
    path, directory = Path(path).resolve(), Path(directory).resolve()
    if not path.is_relative_to(directory) or not path.is_file() or not path.stat().st_size or path.suffix in {".part", ".tmp", ".ytdl"}:
        raise AppError("video_output_invalid", "下载结束，但没有找到有效的成品视频文件。")
    raw = run_process(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], 30, stop=stop)
    try:
        info = json.loads(raw)
        duration = float(info.get("format", {}).get("duration", 0))
        video = next((stream for stream in info.get("streams", [])
                      if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")), None)
    except (ValueError, TypeError, AttributeError) as exc:
        raise AppError("video_output_invalid", "成品视频无法读取，请重新下载。") from exc
    if not video or not math.isfinite(duration) or duration <= 0:
        raise AppError("video_output_invalid", "成品文件缺少有效视频轨道或时长，请重新下载。")
    return path


def download_video(url, quality, directory, on_progress=None, stop=None, source_path=None, proxy=""):
    """保留源格式，允许音画无损合并；返回经过检查的绝对路径。"""
    url = video_url(url)
    if type(quality) is not int or quality <= 0:
        raise AppError("video_link_invalid", "请选择有效的视频画质。")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    on_progress = on_progress or (lambda value: None)

    def receive(line):
        if line.startswith("SF_PROGRESS:"):
            data = json.loads(line.removeprefix("SF_PROGRESS:"))
            total = next((data.get(key) for key in ("total", "estimate")
                          if isinstance(data.get(key), (int, float)) and data[key] > 0), None)
            downloaded = data.get("downloaded")
            percent = min(100, downloaded / total * 100) if isinstance(total, (int, float)) and total > 0 and isinstance(downloaded, (int, float)) else None
            on_progress({"stage": "downloading", "progress": percent, "format_id": data.get("format_id"),
                         "downloaded_bytes": downloaded, "total_bytes": total, "speed": data.get("speed"), "eta": data.get("eta")})
            return True
        if line.startswith("SF_STAGE:"):
            if line.removeprefix("SF_STAGE:") == "Merger":
                on_progress({"stage": "merging", "progress": None})
            return True
        if line.startswith("SF_FILE:"):
            paths.append(json.loads(line.removeprefix("SF_FILE:")))
            return True
        return False

    template = ('download:SF_PROGRESS:{"format_id":%(info.format_id|null)j,"downloaded":%(progress.downloaded_bytes|null)j,'
                '"total":%(progress.total_bytes|null)j,"estimate":%(progress.total_bytes_estimate|null)j,'
                '"speed":%(progress.speed|null)j,"eta":%(progress.eta|null)j}')
    args = [*downloader_args(proxy, url), "--no-simulate", "--force-overwrites", "--progress", "--newline", "--progress-delta", "1",
            "--abort-on-unavailable-fragments", "--format", format_selector(), "--format-sort", f"res:{quality},hasaud", "--format-sort-force",
            "--paths", str(directory), "--output", "%(title).80s-%(id)s-%(height)sP.%(ext)s", "--windows-filenames", "--progress-template", template,
            "--progress-template", "postprocess:SF_STAGE:%(progress.postprocessor)s",
            "--print", "after_move:SF_FILE:%(filepath)j"]
    args += ["--load-info-json", str(source_path)] if source_path is not None else ["--", url]
    try:
        run_process(args, 1800, receive, stop)
    except AppError as error:
        enriched = network_error_hint(url, error, proxy)
        if enriched is error:
            raise
        raise enriched from error
    if len(paths) != 1 or not isinstance(paths[0], str):
        raise AppError("video_output_invalid", "下载器没有返回唯一的成品视频路径。")
    on_progress({"stage": "checking", "progress": None})
    return verify_video(paths[0], directory, stop)
