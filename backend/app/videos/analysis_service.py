import uuid
from sqlalchemy import select
from ..db import Task, Evidence, now, TASK_CONTROL_STATUSES
from ..errors import AppError
from .versions import PROMPT_VERSION, check_generation, digest
from .schemas import FORMAT_VERSION
from .segments import FLOW_VERSION
from ..recognition.local_models import AnalysisInput, analysis_candidate
from ..recognition.extraction import extraction_config
from ..analysis.activity import in_session
from .schemas import current_notes, ground_chapters
from ..records import get_video, get_task
from .serialization import public
from ..api_schemas import NotesPublic
from .task_rules import active_task_query
from ..analysis.markdown import normalize_math
from .notes import completed_notes
from ..settings.model_profiles import execution_profile, for_profile, find, active_id, task_model_config


# 协调视频任务创建、控制和结果读取，实际执行交给共享 Worker。
class AnalysisService:
    def __init__(self, get_settings, sessions, worker):
        self.get_settings = get_settings
        self.sessions = sessions
        self.worker = worker

    def task_config(self, settings=None):
        settings = settings or self.get_settings()
        return {**extraction_config(settings), **task_model_config(settings),
            "generation_version": FORMAT_VERSION, "prompt_version": PROMPT_VERSION,
            "subtitle_policy": "platform-first-v2", "generation_flow": FLOW_VERSION}

    # 固定本次连接与识别配置，复用相同活动任务，提交记录后才入队。
    def analyze(self, video_id: str, value: AnalysisInput | None = None):
        from pathlib import Path
        with self.worker.lock:
            with self.sessions.begin() as session:
                video = get_video(session, video_id)
                current = self.get_settings()
                settings = for_profile(current, find(current, active_id(current)))
                settings = analysis_candidate(settings, value) if value else settings
                config = self.task_config(settings)
                config["video_title"] = Path(video.filename).stem
                if (video.media_info or {}).get("source", {}).get("platform") in {"youtube", "bilibili"}:
                    config["video_source"] = video.media_info["source"]
                config["generation_key"] = digest(config)
                active = session.scalar(active_task_query(video_id))
                if active:
                    check_generation(active.config)
                    if active.config.get("generation_key") != config["generation_key"]:
                        raise AppError("already_running", "该视频已有不同配置的任务，请先停止当前任务。", 409)
                    return {"task_id": active.id, "status": active.status, "reused": True}
                task = Task(id=uuid.uuid4().hex, video_id=video_id, config=config)
                session.add(task)
                session.flush()
                in_session(session, task, getattr(self.worker, "diagnostics", None))
                task_id = task.id
            self.worker.submit("video", task_id)
        return {"task_id": task_id, "status": "queued", "reused": False}

    def analysis_preview(self, video_id, value):
        from .preview import analysis_preview
        from ..recognition.extraction import extraction_signature
        with self.sessions() as session:
            video = get_video(session, video_id)
            current = self.get_settings()
            settings = analysis_candidate(for_profile(current, find(current, active_id(current))), value)
            config = extraction_config(settings)
            signature = extraction_signature(config)
            for previous in session.scalars(select(Task.config).where(Task.video_id == video_id,
                    Task.config["execution_started"].as_boolean().is_(True)).order_by(Task.created_at.desc())):
                if extraction_signature({key: item for key, item in previous.items() if key != "transcript_source"}) == signature:
                    if previous.get("transcript_source"):
                        config["transcript_source"] = previous["transcript_source"]
                    break
            return analysis_preview(video, settings.data_dir / "videos" / video_id, config, settings)

    def task_status(self, task_id: str):
        with self.sessions() as session:
            return public(get_task(session, task_id))

    def versions(self, video_id: str):
        with self.sessions() as session:
            get_video(session, video_id)
            return [public(task) for task in completed_notes(session, video_id)[:2]]

    def stop_task(self, task_id: str):
        with self.worker.lock:
            with self.sessions.begin() as session:
                # 与工作线程完成任务的写入串行，避免把已完成任务覆盖成停止中。
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = get_task(session, task_id)
                if task.status not in TASK_CONTROL_STATUSES["stop"]:
                    raise AppError("not_stoppable", "该任务已经结束，不能停止。", 409)
                if task.status == "queued":
                    task.status = "paused"
                elif task.status == "running":
                    task.status = "stopping"
                task.updated_at = now()
                in_session(session, task, getattr(self.worker, "diagnostics", None))
                result = {"task_id": task.id, "status": task.status}
        self.worker.cancel("video", task_id)
        return result

    def resume_task(self, task_id: str):
        with self.worker.lock:
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = get_task(session, task_id)
                if task.status not in TASK_CONTROL_STATUSES["resume"]:
                    raise AppError("not_resumable", "请等待任务停止完成后再继续。", 409)
                active = session.scalar(active_task_query(task.video_id).with_only_columns(Task.id))
                if active:
                    raise AppError("already_running", "同一视频已经有任务正在处理。", 409)
                check_generation(task.config)
                execution_profile(self.get_settings(), task.config)
                task.status, task.error = "queued", None
                task.attempts += 1
                task.updated_at = now()
                in_session(session, task, getattr(self.worker, "diagnostics", None), message="继续处理，排队中")
            self.worker.submit("video", task_id)
        return {"task_id": task_id, "status": "queued"}

    def retry(self, task_id: str):
        with self.worker.lock:
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = get_task(session, task_id)
                if task.status not in TASK_CONTROL_STATUSES["retry"]:
                    raise AppError("not_retryable", "只能重试失败或中断的任务。", 409)
                active = session.scalar(active_task_query(task.video_id))
                if active:
                    raise AppError("already_running", "同一视频已经有任务正在处理。", 409)
                check_generation(task.config)
                execution_profile(self.get_settings(), task.config)
                task.status, task.stage, task.error = "queued", "queued", None
                task.attempts += 1
                task.updated_at = now()
                in_session(session, task, getattr(self.worker, "diagnostics", None), message="重试任务，排队中")
            self.worker.submit("video", task_id)
        return {"task_id": task_id, "status": "queued"}

    def analysis(self, analysis_id: str):
        with self.sessions() as session:
            task = get_task(session, analysis_id)
            if task.status != "succeeded" and (not current_notes(task.result) or task.result.get("complete", True)):
                raise AppError("not_ready", "分析尚未成功完成，请先查看任务状态。", 409)
            if not current_notes(task.result):
                raise AppError("not_ready", "暂无可用笔记。", 409)
            video = get_video(session, task.video_id)
            evidence = {item.id: public(item) for item in session.scalars(
                select(Evidence).where(Evidence.video_id == video.id, Evidence.id.in_(task.result.get("source_ids", task.result["evidence_ids"]))))}
            complete = task.result.get("complete", True)
            covered = video.duration if complete else task.result["covered_until_seconds"]
            if task.result.get("note_style") == "concise":
                from .concise import validate_result
                chapters = validate_result(task.result, evidence, video.duration)
            else:
                chapters = ground_chapters(task.result["chapters"], task.result["source_issues"], evidence, video.duration, end=covered)
            chapters = [{**chapter, "markdown": normalize_math(chapter["markdown"])} for chapter in chapters]
            return NotesPublic.model_validate({"id": task.id, "video_id": task.video_id, "warnings": task.warnings,
                    "timings": task.timings, **task.result, "chapters": chapters,
                    "complete": complete, "covered_until_seconds": covered}).model_dump(exclude_unset=True)
