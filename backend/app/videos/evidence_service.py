from pathlib import Path
from typing import Literal
from sqlalchemy import select
from ..db import Evidence
from ..errors import AppError
from ..recognition.extraction import extraction_signature
from ..records import get_video, get_task
from .serialization import public
from .transcripts import transcript_contents
from .notes import notes_text, notes_markdown


# 按任务来源快照选择依据与转录，不因读取接口重新执行识别。
class EvidenceService:
    def __init__(self, get_settings, sessions, analyses):
        self.get_settings = get_settings
        self.sessions = sessions
        self.analyses = analyses

    def video_evidence(self, video_id: str, task_id: str | None = None):
        with self.sessions() as session:
            get_video(session, video_id)
            query = select(Evidence).where(Evidence.video_id == video_id)
            if task_id:
                task = get_task(session, task_id)
                if task.video_id != video_id:
                    raise AppError("wrong_video", "该任务不属于当前视频。", 404)
                signature = extraction_signature(task.config)
                query = query.where(Evidence.id.startswith(f"{video_id}-{signature}-"))
            return [public(e) for e in session.scalars(query.order_by(Evidence.start_seconds))]

    def transcript(self, video_id: str, task_id: str, format: Literal["txt", "json"] = "txt"):
        with self.sessions() as session:
            video = get_video(session, video_id)
            task = get_task(session, task_id)
            if task.video_id != video_id:
                raise AppError("wrong_video", "该任务不属于当前视频。", 404)
            signature = extraction_signature(task.config)
            query = select(Evidence).where(Evidence.video_id == video_id, Evidence.id.startswith(f"{video_id}-{signature}-"))
            items = [public(item) for item in session.scalars(query)]
            if not items:
                raise AppError("not_ready", "转录尚未保存，请等待识别步骤完成。", 409)
            content = transcript_contents(video_id, signature, items, filename=video.filename, duration=video.duration,
                                          transcript_source=task.config.get("transcript_source"))[format]
            filename = f"{Path(video.filename).stem}-原始转录.{format}"
        return content, filename

    def download_notes(self, analysis_id: str, format: Literal["txt", "md"] = "txt"):
        result = self.analyses.analysis(analysis_id)
        with self.sessions() as session:
            video = get_video(session, result["video_id"])
            content = (notes_markdown if format == "md" else notes_text)(video.filename, video.duration, result)
            filename = f"{Path(video.filename).stem}-视频笔记.{format}"
        return content, filename

    def evidence(self, evidence_id: str):
        with self.sessions() as session:
            item = session.get(Evidence, evidence_id)
            if not item:
                raise AppError("not_found", "证据不存在。", 404)
            return public(item)

    def evidence_image(self, evidence_id: str):
        with self.sessions() as session:
            item = session.get(Evidence, evidence_id)
            if not item or not item.image_name:
                raise AppError("not_found", "这条证据没有截图。", 404)
            path = self.get_settings().data_dir / "videos" / item.video_id / item.image_name
        if not path.is_file():
            raise AppError("not_found", "截图文件不存在。", 404)
        return path
