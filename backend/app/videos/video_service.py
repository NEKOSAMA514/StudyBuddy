import shutil
import uuid
from pathlib import Path
from threading import Lock
from ..util.file_contracts import FileEdit
from sqlalchemy import case, select, delete
from ..db import Video, Task, Evidence, ACTIVE_TASK_STATUSES
from ..errors import AppError
from ..util.file_ingest import save_stream
from .video_reader import VIDEO_EXTENSIONS, probe, source_path, direct_playback
from ..records import get_video
from ..note_marks import clear_marks
from .serialization import public
from .task_rules import active_task_query
from ..video_download.subtitles import youtube_source



# 管理视频上传、去重和文件操作，目录与任务状态在共享锁内协调。
class VideoService:
    def __init__(self, get_settings, sessions, playback, video_downloads, diagnostics, operation_lock):
        self.get_settings = get_settings
        self.sessions = sessions
        self.playback = playback
        self.video_downloads = video_downloads
        self.diagnostics = diagnostics
        self.operation_lock = operation_lock
        self.upload_lock = Lock()

    def list_videos(self):
        with self.sessions() as session:
            result = []
            for video in session.scalars(select(Video).order_by(Video.pinned.desc(), Video.created_at.desc(), Video.id)):
                task = session.scalar(select(Task).where(Task.video_id == video.id)
                                      .order_by(case((Task.status.in_(ACTIVE_TASK_STATUSES), 0), else_=1),
                                                Task.updated_at.desc(), Task.id.desc()).limit(1))
                result.append({**public(video), "latest_task": public(task) if task else None})
            result.sort(key=lambda video: (video["pinned"], video["latest_task"]["created_at"]
                                          if video["latest_task"] else video["created_at"]), reverse=True)
            return result

    def edit_video(self, video_id: str, value: FileEdit):
        with self.sessions.begin() as session:
            video = get_video(session, video_id)
            for key, item in value.model_dump(exclude_none=True).items():
                setattr(video, key, item)
            session.flush()
            return public(video)

    def delete_video(self, video_id: str):
        with self.operation_lock:
            settings = self.get_settings()
            root = (settings.data_dir / "videos").resolve()
            folder = (root / video_id).resolve()
            if folder.parent != root:
                raise AppError("invalid_video", "视频编号无效。", 400)
            trash = settings.data_dir / "_deleting" / uuid.uuid4().hex
            moved = False
            try:
                with self.sessions.begin() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    get_video(session, video_id)
                    active = session.scalar(active_task_query(video_id).with_only_columns(Task.id))
                    if active:
                        raise AppError("video_in_use", "请先停止该视频的处理任务，等待停止完成后再删除。", 409)
                    self.playback.cancel(video_id)
                    if folder.exists():
                        trash.parent.mkdir(exist_ok=True)
                        folder.rename(trash)
                        moved = True
                    session.execute(delete(Evidence).where(Evidence.video_id == video_id))
                    clear_marks(session, "video", select(Task.id).where(Task.video_id == video_id))
                    session.execute(delete(Task).where(Task.video_id == video_id))
                    session.delete(get_video(session, video_id))
            except BaseException:
                if moved:
                    trash.rename(folder)
                raise
            warning = None
            if moved:
                try:
                    shutil.rmtree(trash)
                except OSError as exc:
                    warning = self.diagnostics.record("delete_cleanup_failed", "视频记录已删除，部分文件未能清理；请按日志检查 data/_deleting。", exc, video_id=video_id)
            return {"deleted": True, "warning": warning}

    def register_video(self, stream, filename):
        settings = self.get_settings()
        filename = (filename or "video").replace("\\", "/").split("/")[-1]
        extension = Path(filename).suffix.lower().lstrip(".")
        if extension not in VIDEO_EXTENSIONS:
            raise AppError("unsupported_extension", "请选择支持的视频文件。")
        video_id = uuid.uuid4().hex
        folder = settings.data_dir / "videos" / video_id
        folder.mkdir()
        path = folder / f"original.{extension}"
        try:
            saved = save_stream(stream, path, settings.max_upload_bytes)
            info = probe(path, settings.max_duration)
            # 存储格式和显示类型以检测出的容器为准，不随可编辑标题改变。
            actual = folder / f'original.{info["media_info"]["container"]}'
            if actual != path:
                path.rename(actual)
            info["media_info"]["source_name"] = actual.name
            with self.upload_lock:
                with self.sessions.begin() as session:
                    existing = session.scalar(select(Video).where(Video.fingerprint == saved["fingerprint"]))
                    if existing:
                        shutil.rmtree(folder)
                        return {**public(existing), "reused": True}
                    row = Video(id=video_id, filename=filename[:200], **saved, **info)
                    session.add(row)
                    session.flush()
                    return {**public(row), "reused": False}
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def import_download(self, identifier: str):
        task = self.video_downloads.get(identifier)
        if task["status"] != "succeeded":
            raise AppError("not_ready", "请等待视频下载完成后再开始生成笔记。", 409)
        source = Path(task.get("path") or "").resolve()
        if (source.parent != self.video_downloads.locations[identifier].resolve() or
                source.parent.parent != self.video_downloads.directory or not source.is_file()):
            raise AppError("file_missing", "下载文件不存在或已被移动，请重新下载。", 404)
        # 复用本地保存、媒体校验与去重；无需浏览器重新上传成品。
        with source.open("rb") as file:
            result = self.register_video(file, source.name)
        origin = (task.get("metadata") or {}).get("source") or youtube_source(task.get("url", ""))
        if origin:
            with self.sessions.begin() as session:
                video = get_video(session, result["id"])
                video.media_info = {**(video.media_info or {}), "source": origin}
                session.flush()
                result = {**public(video), "reused": result["reused"]}
        return result

    def video_source(self, video_id):
        with self.sessions() as session:
            row = get_video(session, video_id)
            source = source_path(self.get_settings().data_dir / "videos" / video_id, row.media_info)
            media = row.media_info or probe(source, validate=False)["media_info"]
            return source, media

    def playback_info(self, video_id, source, media):
        state = self.playback.status(video_id, source)
        return {**state, "original_url": f"/api/v1/videos/{video_id}/content",
                "mime_type": media["mime_type"], "direct": direct_playback(media),
                "url": f"/api/v1/videos/{video_id}/playback/content" if state["status"] == "ready" else None}

    def video_content(self, video_id: str):
        source, media = self.video_source(video_id)
        return source, media["mime_type"]

    def get_playback(self, video_id: str):
        source, media = self.video_source(video_id)
        return self.playback_info(video_id, source, media)

    def start_playback(self, video_id: str):
        # 与删除操作共用锁，防止后台线程开始读取已删除的原件。
        with self.operation_lock:
            source, media = self.video_source(video_id)
            self.playback.prepare(video_id, source, media)
            return self.playback_info(video_id, source, media)

    def playback_content(self, video_id: str):
        source, _ = self.video_source(video_id)
        if self.playback.status(video_id, source)["status"] != "ready":
            raise AppError("not_ready", "播放视频尚未准备完成。", 409)
        return self.playback.output(source)
