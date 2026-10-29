"""文档任务生命周期、生成预检及执行配置。"""
import uuid
from dataclasses import replace
from sqlalchemy import select
from ..db import DocumentTask, now, ACTIVE_TASK_STATUSES, TASK_CONTROL_STATUSES
from ..errors import AppError
from ..settings.model_profiles import for_profile, find, task_model_config, execution_settings, EXECUTION_FIELDS
from .material import (load_material, verify_snapshot, snapshot, detailed_segments,
                       MAX_DOCUMENT_CHARACTERS, MAX_DOCUMENT_SEGMENTS, MAX_DOCUMENT_REQUESTS)
from .schemas import public_task
from .notes import DocumentNotes
from .task_rules import get_task
from .versions import check_generation, DOCUMENT_PROMPT_VERSION, FLOW_VERSION, generation_key
from ..analysis.activity import in_session


# 用与实际请求一致的材料分段检查生成资格，不发送模型请求。
def preview(material, settings, title=""):
    from .model import DocumentModelClient
    source = snapshot(material)
    if source["character_count"] > MAX_DOCUMENT_CHARACTERS:
        return {"source": source, "character_count": source["character_count"], "estimated_segments": 0,
            "eligible": False, "reason": "文档超过10万字符，请拆分文件后生成；仍可查看提取内容。",
            "limits": {"characters": MAX_DOCUMENT_CHARACTERS, "segments": MAX_DOCUMENT_SEGMENTS, "requests": MAX_DOCUMENT_REQUESTS}}
    model = DocumentModelClient(settings)
    try:
        if settings.note_style == "concise":
            from .concise import plan
            segments = plan(material, model, title)
            fragments = [p for s in segments for p in s["primary"]]
        else:
            fragments, segments = detailed_segments(material, model, title)
    except AppError as exc:
        if exc.code not in {"model_input_budget", "no_evidence"}:
            raise
        return {"source": source, "character_count": source["character_count"], "estimated_segments": 0,
            "eligible": False, "reason": exc.message,
            "limits": {"characters": MAX_DOCUMENT_CHARACTERS, "segments": MAX_DOCUMENT_SEGMENTS, "requests": MAX_DOCUMENT_REQUESTS}}
    reason = ("没有可用于生成笔记的文字。" if not fragments else
              "文档超过10万字符或8个片段，请拆分文件后生成；仍可查看提取内容。"
              if source["character_count"] > MAX_DOCUMENT_CHARACTERS or len(segments) > MAX_DOCUMENT_SEGMENTS else None)
    return {"source": source, "character_count": source["character_count"], "estimated_segments": len(segments),
            "eligible": reason is None, "reason": reason,
            "limits": {"characters": MAX_DOCUMENT_CHARACTERS, "segments": MAX_DOCUMENT_SEGMENTS, "requests": MAX_DOCUMENT_REQUESTS}}


# 固定材料身份、执行参数与提示词版本，继续任务时不采用新的默认设置。
def task_config(settings, title, info):
    config = {**task_model_config(settings), "source": info["source"], "document_title": title,
              "prompt_version": DOCUMENT_PROMPT_VERSION,
              "total_segments": info["estimated_segments"]}
    config["generation_flow"] = FLOW_VERSION
    config["generation_key"] = generation_key(config)
    return config


# 协调文档任务的预检、幂等创建和停止恢复，材料读写交给 Store。
class DocumentAnalyses:
    def __init__(self, documents, get_settings, sessions, worker):
        self.documents, self.get_settings, self.sessions, self.worker = documents, get_settings, sessions, worker
        self.notes = DocumentNotes(documents, sessions)

    def material(self, session, identifier):
        document = self.documents.get(session, identifier)
        if document.fingerprint.startswith("pending:"):
            raise AppError("document_not_prepared", "文档尚未提取，请点击开始处理。", 409)
        return load_material(self.documents.folder(identifier))

    def analysis_preview(self, identifier, llm_profile_id=None, note_style=None):
        with self.worker.lock, self.sessions() as session:
            current = self.get_settings()
            settings = for_profile(current, find(current, llm_profile_id)) if llm_profile_id else current
            settings = replace(settings, note_style=note_style or settings.note_style)
            document = self.documents.get(session, identifier)
            return preview(self.material(session, identifier), settings, document.filename)

    # 预分配编号和相同活动配置都可复用，数据库提交后再加入共享队列。
    def create(self, identifier, value, *, task_id=None, settings=None):
        with self.worker.lock, self.sessions.begin() as session:
            if task_id and (existing := session.get(DocumentTask, task_id)):
                if existing.document_id != identifier:
                    raise AppError("invalid_document", "笔记任务与资料不一致。")
                return {"task_id": existing.id, "status": existing.status, "reused": True}
            document = self.documents.get(session, identifier)
            material = self.material(session, identifier)
            if settings is None:
                current = self.get_settings()
                settings = for_profile(current, find(current, value.llm_profile_id)) if value and value.llm_profile_id else current
            settings = replace(settings, note_style=value.note_style if value and value.note_style else settings.note_style)
            info = preview(material, settings, document.filename)
            if not info["eligible"]:
                raise AppError("document_generation_limit", info["reason"])
            if not settings.llm_key:
                raise AppError("model_not_configured", "请先在设置中填写所选 API 配置的密钥。")
            config = task_config(settings, document.filename, info)
            active = session.scalar(select(DocumentTask).where(DocumentTask.document_id == identifier, DocumentTask.status.in_(ACTIVE_TASK_STATUSES)))
            if active:
                check_generation(active.config)
                if active.config["generation_key"] != config["generation_key"]:
                    raise AppError("already_running", "此文档已有不同配置的任务，请先停止当前任务。", 409)
                return {"task_id": active.id, "status": active.status, "reused": True}
            task = DocumentTask(id=task_id or uuid.uuid4().hex, document_id=identifier, config=config)
            session.add(task)
            session.flush()
            in_session(session, task, getattr(self.worker, "diagnostics", None))
            task_id = task.id
        self.worker.submit("document", task_id)
        return {"task_id": task_id, "status": "queued", "reused": False}

    def status(self, identifier):
        with self.sessions() as session:
            return public_task(get_task(session, identifier))

    # 恢复前检查原材料与执行快照，继续和重试更新入队时间并排到队尾。
    def control(self, identifier, action):
        with self.worker.lock, self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = get_task(session, identifier)
            if task.status not in TASK_CONTROL_STATUSES[action]:
                raise AppError("invalid_document_task_state", "任务当前状态不允许此操作。", 409)
            if action == "stop":
                task.status = "stopping" if task.status in {"running", "stopping"} else "paused"
            else:
                verify_snapshot(self.material(session, task.document_id), task.config["source"])
                check_generation(task.config)
                if task.request_count >= MAX_DOCUMENT_REQUESTS:
                    raise AppError("document_request_limit", "已达到80次请求上限，请拆分文档后重新生成。", 409)
                execution_settings(self.get_settings(), task.config, required_fields=EXECUTION_FIELDS)
                active = session.scalar(select(DocumentTask.id).where(DocumentTask.document_id == task.document_id,
                                                                       DocumentTask.status.in_(ACTIVE_TASK_STATUSES)))
                if active:
                    raise AppError("already_running", "此文档已有任务正在处理。", 409)
                task.status, task.error, task.attempts = "queued", None, task.attempts + 1
            task.updated_at = now()
            in_session(session, task, getattr(self.worker, "diagnostics", None),
                       **({"message": "继续处理，排队中" if action == "resume" else "重试任务，排队中"} if action != "stop" else {}))
            result = {"task_id": task.id, "status": task.status}
        if action == "stop":
            self.worker.cancel("document", identifier)
        else:
            self.worker.submit("document", identifier)
        return result
