"""本地视频读写，不依赖应用记录、识别器或模型服务。"""
import json
import math
import uuid
import wave
from pathlib import Path
from ..errors import AppError
from ..util.media_process import command

VIDEO_EXTENSIONS = ("mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "wmv", "asf",
                    "mpg", "mpeg", "vob", "ts", "mts", "m2ts", "3gp", "3g2", "ogv", "rm", "rmvb", "mxf")
MIMES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
         "mkv": "video/x-matroska", "avi": "video/x-msvideo", "flv": "video/x-flv",
         "wmv": "video/x-ms-wmv", "asf": "video/x-ms-asf", "mpg": "video/mpeg",
         "ts": "video/mp2t", "3gp": "video/3gpp", "3g2": "video/3gpp2", "ogv": "video/ogg",
         "rm": "application/vnd.rn-realmedia", "mxf": "application/mxf"}


# 根据检测到的容器格式定位原件，不依赖用户可改的文件标题。
def source_path(folder: Path, media_info=None):
    name = (media_info or {}).get("source_name", "original.mp4")
    path = (folder / name).resolve()
    if path.parent != folder.resolve() or not path.is_file():
        raise AppError("file_missing", "本地视频文件已不存在。", 404)
    return path


def container_name(info, path):
    names = info["format"]["format_name"].split(",")
    brand = info["format"].get("tags", {}).get("major_brand", "").strip()
    if "mp4" in names:
        if brand == "qt":
            return "mov"
        return "3g2" if brand.startswith("3g2") else "3gp" if brand.startswith("3g") else "mp4"
    if "matroska" in names:
        # 两种容器共用解复用器，通过 EBML DocType 判断真实格式。
        with path.open("rb") as source:
            header = source.read(4096)
        return "webm" if b"\x42\x82\x84webm" in header else "mkv"
    return {"mpegts": "ts", "mpeg": "mpg", "asf": "asf", "ogg": "ogv"}.get(names[0], names[0])


def probe(path: Path, max_duration=float("inf"), validate=True, cancel=None):
    raw = command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], 30, cancel)
    try:
        info = json.loads(raw)
        streams = info.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video" and
                     not s.get("disposition", {}).get("attached_pic") and s.get("width", 0) > 0 and s.get("height", 0) > 0)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration = float(info["format"]["duration"])
        start = float(info["format"].get("start_time", 0))
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise AppError("invalid_media", "没有找到有效的视频轨道或时长。") from exc
    container = container_name(info, path)
    if container not in MIMES:
        raise AppError("invalid_media", "文件内容不是支持的视频格式。")
    ends = [float(track["start_time"]) + float(track["duration"]) for track in (video, audio)
            if track and track.get("start_time") not in (None, "N/A") and track.get("duration") not in (None, "N/A")]
    if ends and math.isfinite(start):
        duration = max(ends) - start
    elif container in {"mkv", "webm", "flv"} and math.isfinite(start) and start > 0:
        duration -= start
    if not math.isfinite(duration) or duration <= 0:
        raise AppError("invalid_media", "没有找到有效的视频轨道或时长。")
    if duration > max_duration:
        raise AppError("duration_limit", f"视频时长必须在 0 到 {int(max_duration)} 秒之间。")
    tracks = ["-map", f'0:{video["index"]}'] + (["-map", f'0:{audio["index"]}'] if audio else [])
    if validate:
        command(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), *tracks,
                 "-t", "1", "-f", "null", "-"], 60, cancel)
    media = {"source_name": path.name, "container": container, "mime_type": MIMES.get(container, "application/octet-stream"),
             "video_tracks": sum(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams),
             "audio_tracks": sum(s.get("codec_type") == "audio" for s in streams),
             "start_time": start if math.isfinite(start) else 0,
             "video": {key: video.get(key) for key in ("index", "codec_name", "pix_fmt", "width", "height", "start_time", "avg_frame_rate")},
             "audio": {key: audio.get(key) for key in ("index", "codec_name", "start_time", "sample_rate", "channels")} if audio else None}
    return {"duration": duration, "video_codec": video["codec_name"],
            "audio_codec": audio["codec_name"] if audio else None, "media_info": media}


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise AppError("media_cancelled", "媒体处理已取消。")


def extract_audio(video: Path, output: Path, media_info=None, cancel=None):
    check_cancel(cancel)
    media = media_info or probe(video, validate=False, cancel=cancel)["media_info"]
    if media["audio"] is None:
        raise AppError("invalid_media", "该视频没有音轨。")
    # 音频时间戳与画面定位使用同一时间原点，延迟开始的音轨补静音。
    temporary = output.with_name(f"{output.stem}-{uuid.uuid4().hex}.tmp.wav")
    try:
        command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-copyts", "-start_at_zero", "-i", str(video),
                 "-map", f'0:{media["audio"]["index"]}', "-vn", "-af", "aresample=async=1:first_pts=0",
                 "-ar", "16000", "-ac", "1", str(temporary)], cancel=cancel)
        with wave.open(str(temporary), "rb") as source:
            if source.getnframes() <= 0:
                raise AppError("invalid_media", "提取的音频没有有效内容。")
        check_cancel(cancel)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def extract_frame(video: Path, output: Path, seconds: float, media_info=None, cancel=None):
    from PIL import Image
    check_cancel(cancel)
    media = media_info or probe(video, validate=False, cancel=cancel)["media_info"]
    temporary = output.with_name(f"{output.stem}-{uuid.uuid4().hex}.tmp.jpg")
    try:
        command(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(max(0, seconds)), "-i", str(video),
                 "-map", f'0:{media["video"]["index"]}', "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2", str(temporary)], 60, cancel)
        if not temporary.is_file():
            raise AppError("frame_missing", "该时间位置未能提取画面。")
        with Image.open(temporary) as image:
            image.verify()
        check_cancel(cancel)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


# 复用同一解码器按计划时间取图，避免每个采样点单独启动进程。
class UniformFrameReader:
    """共用一个解码器按时间定位，避免每张截图都启动一次外部进程。"""
    def __init__(self, video, media_info):
        self.video, self.media = video, media_info or {}
        self.container = None

    def save(self, output, seconds, cancel=None):
        import av
        from PIL import Image
        check_cancel(cancel)
        if self.container is None:
            self.container = av.open(str(self.video), options={"protocol_whitelist": "file,pipe"})
            index = self.media.get("video", {}).get("index")
            self.stream = next(s for s in self.container.streams.video if (index is None or s.index == index)
                               and not s.disposition & s.disposition.attached_pic)
            self.stream.thread_type = "AUTO"
        default_origin = self.container.start_time / av.time_base if self.container.start_time is not None else 0
        origin = self.media.get("start_time", default_origin)
        self.container.seek(int((origin + seconds) / self.stream.time_base), stream=self.stream, backward=True)
        selected = None
        for frame in self.container.decode(self.stream):
            check_cancel(cancel)
            if frame.time is None:
                raise AppError("frame_missing", "视频帧缺少时间信息。")
            selected = frame
            if float(frame.time) - origin >= seconds - 1e-6:
                break
        if selected is None:
            raise AppError("frame_missing", "该时间位置未能提取画面。")
        # 片尾目标可能晚于最后一帧的起始时间，此时使用最后一帧。
        image = selected.to_image()
        if selected.rotation:
            image = image.rotate(selected.rotation, expand=True)
        if image.width > 1920:
            image = image.resize((1920, max(2, round(image.height * 1920 / image.width) // 2 * 2)), Image.Resampling.LANCZOS)
        temporary = output.with_name(uuid.uuid4().hex + ".tmp")
        try:
            image.convert("RGB").save(temporary, format="JPEG", quality=92)
            check_cancel(cancel)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        if self.container is not None:
            self.container.close()


def compatible_tracks(media):
    return (media["video"]["codec_name"] == "h264" and media["video"].get("pix_fmt") in {"yuv420p", "yuvj420p"}
            and (media["audio"] is None or media["audio"]["codec_name"] == "aac"))


def direct_playback(media):
    if media.get("video_tracks", 1) > 1 or media.get("audio_tracks", 1) > 1:
        return False
    mp4_tracks = compatible_tracks(media) or (
        media["video"]["codec_name"] in {"av1", "vp9"} and
        media["video"].get("pix_fmt") in {"yuv420p", "yuv420p10le"} and
        (media["audio"] is None or media["audio"]["codec_name"] == "aac"))
    return ((media["container"] == "mp4" and mp4_tracks) or
            (media["container"] == "webm" and media["video"]["codec_name"] in {"vp8", "vp9", "av1"} and
             (media["audio"] is None or media["audio"]["codec_name"] in {"opus", "vorbis"})))


# 浏览器无法直接播放原件时准备兼容副本，原件仍供识别和下载使用。
def prepare_playback(source: Path, output: Path, media_info=None, cancel=None):
    media = media_info or probe(source, validate=False, cancel=cancel)["media_info"]
    temporary = output.with_name(output.stem + ".tmp.mp4")
    maps = ["-map", f'0:{media["video"]["index"]}']
    if media["audio"] is not None:
        maps += ["-map", f'0:{media["audio"]["index"]}']
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-copyts", "-start_at_zero", "-i", str(source), *maps, "-sn", "-dn"]
    method = "transcode"
    try:
        if compatible_tracks(media) and (media["container"] != "mp4" or media.get("video_tracks", 1) > 1 or media.get("audio_tracks", 1) > 1):
            try:
                command([*base, "-c", "copy", "-movflags", "+faststart", str(temporary)], 1800, cancel)
                probe(temporary, cancel=cancel)
                method = "remux"
            except AppError as exc:
                if exc.code not in {"invalid_media"}:
                    raise
        if method == "transcode":
            command([*base, "-vf", "scale=w='trunc(min(1280,min(iw,min(720,ih)*dar))/2)*2':h='trunc(min(720,min(ih,min(1280,iw)/dar))/2)*2',setsar=1",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "2",
                     "-c:a", "aac", "-b:a", "128k", "-fps_mode", "vfr", "-movflags", "+faststart", str(temporary)], 1800, cancel)
            probe(temporary, cancel=cancel)
        if cancel is not None and cancel.is_set():
            raise AppError("media_cancelled", "播放视频准备已取消。")
        temporary.replace(output)
        return method
    finally:
        temporary.unlink(missing_ok=True)
