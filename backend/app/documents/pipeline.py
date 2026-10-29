"""单个文档任务执行、取消、成功提交与检查点补清。"""
import asyncio
import threading
import time
from sqlalchemy import update
from ..db import DocumentTask, now
from ..errors import AppError, TaskInterrupted, DocumentStopped
from ..analysis.checkpoints import CheckpointCleanup
from ..analysis.activity import TaskActivity, in_session
from .detailed import DetailedDocumentGeneration
from .material import load_material, verify_snapshot, MAX_DOCUMENT_REQUESTS
from .model import DocumentModelClient
from .versions import check_generation
from ..settings.model_profiles import execution_settings, EXECUTION_FIELDS
from .notes import retain_two


class DocumentPipeline:
    recovery_message = "上次处理已中断，可手动重试；成功章节会复用。"
    def __init__(self, get_settings, sessions, diagnostics, model_factory=DocumentModelClient):
        self.get_settings, self.sessions, self.diagnostics, self.model_factory = get_settings, sessions, diagnostics, model_factory
        self.checkpoint_cleanup = CheckpointCleanup(sessions, DocumentTask, get_settings().data_dir / "documents", lambda task: task.document_id, diagnostics)
        self.control_lock, self.model_control = threading.Lock(), None
        self.shutdown = None

    # 分别检测用户停止与服务退出，让上层保存草稿并记录正确的终止状态。
    def check(self, identifier):
        with self.sessions() as session:
            task = session.get(DocumentTask, identifier)
            if task is None or task.status != "running":
                raise DocumentStopped()
        if self.shutdown is not None and self.shutdown.is_set():
            raise TaskInterrupted()

    def cancel_model(self, identifier):
        with self.control_lock:
            if self.model_control and self.model_control[0] == identifier:
                self.model_control[1].call_soon_threadsafe(self.model_control[2].set)

    # 每次实际模型请求前原子占用次数，重试与修正同样计入上限。
    def reserve_request(self, identifier):
        self.check(identifier)
        with self.sessions.begin() as session:
            changed = session.execute(update(DocumentTask).where(DocumentTask.id == identifier,
                DocumentTask.status == "running", DocumentTask.request_count < MAX_DOCUMENT_REQUESTS)
                .values(request_count=DocumentTask.request_count + 1)).rowcount
            if not changed:
                task = session.get(DocumentTask, identifier)
                if not task or task.status != "running":
                    raise DocumentStopped()
                raise AppError("document_request_limit", "已达到80次请求上限，草稿已保留，请拆分文档后重新生成。", 409)

    def stage(self, identifier, stage, number, total, summary=None):
        self.check(identifier)
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(DocumentTask, identifier)
            task.stage, task.updated_at = stage, now()
            task.config = {**task.config, "segment": number, "total_segments": total,
                           **({"outline_preview": summary} if summary else {})}
            in_session(session, task, self.diagnostics)

    # 分段完成后发布可读草稿并递增正文修订号，停止中的已完成内容仍可保存。
    def publish(self, identifier, value):
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(DocumentTask, identifier)
            if task and task.status in {"running", "stopping"}:
                task.notes_revision += 1
                task.result = {**value, "notes_revision": task.notes_revision}
                task.updated_at = now()
                in_session(session, task, self.diagnostics, "saved", message="本段笔记已校验并保存")

    # 成功结果、两份版本保留及检查点清理标志在同一事务内提交。
    def succeed(self, identifier, result, timings, usage):
        self.check(identifier)
        with self.sessions.begin() as session:
            # 在同一写事务内串行确认成功或停止，并执行版本保留。
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(DocumentTask, identifier)
            if not task or task.status != "running":
                raise DocumentStopped()
            task.status, task.stage = "succeeded", "completed"
            task.updated_at = task.completed_at = now()
            task.timings, task.error = timings, None
            task.notes_revision += 1
            task.result = {**result, "notes_revision": task.notes_revision, "usage": usage}
            task.config = {**task.config, "checkpoint_cleanup_pending": True}
            in_session(session, task, self.diagnostics)
            session.flush()
            retain_two(session, task.document_id)

    # 按任务风格选择生成器，并将跨线程停止信号转为异步取消。
    async def generate(self, task, settings, folder, material, timings=None):
        timings = timings if timings is not None else {}
        stop = asyncio.Event()
        with self.control_lock:
            self.model_control = task.id, asyncio.get_running_loop(), stop
        try:
            self.check(task.id)
            async with self.model_factory(settings) as model:
                model.diagnostics = self.diagnostics
                model.activity = TaskActivity(self.sessions, DocumentTask, task.id, task.attempts, self.diagnostics)
                model.before_request = lambda: self.reserve_request(task.id)
                model.check_stop = lambda: self.check(task.id)
                from .concise import ConciseDocumentGeneration
                factory = ConciseDocumentGeneration if settings.note_style == "concise" else DetailedDocumentGeneration
                generation = factory(task.id, folder, task.config, material, model, stop,
                    lambda: self.check(task.id), lambda *args: self.stage(task.id, *args),
                    lambda value: self.publish(task.id, value))
                generation.timings = timings
                active, stopped = asyncio.create_task(generation.run()), asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait([active, stopped], return_when=asyncio.FIRST_COMPLETED)
                    if stop.is_set():
                        self.check(task.id)
                        raise DocumentStopped()
                    return await active, generation.timings, model.usage
                finally:
                    active.cancel()
                    stopped.cancel()
                    await asyncio.gather(active, stopped, return_exceptions=True)
        finally:
            with self.control_lock:
                self.model_control = None

    # 领取排队任务后核对材料和配置快照，统一处理成功、停止、中断与失败。
    def run(self, identifier, *, shutdown=None):
        if shutdown is not None and shutdown.is_set():
            return
        start = time.monotonic()
        timings = {}
        with self.sessions.begin() as session:
            task = session.get(DocumentTask, identifier)
            if not task or task.status != "queued":
                return
            try:
                check_generation(task.config)
            except AppError as exc:
                task.status, task.error, task.updated_at = "failed", {"code": exc.code, "message": exc.message}, now()
                return
            if not session.execute(update(DocumentTask).where(DocumentTask.id == identifier, DocumentTask.status == "queued")
                                   .values(status="running", stage="checking", updated_at=now())).rowcount:
                return
        try:
            self.shutdown = shutdown
            with self.sessions() as session:
                task = session.get(DocumentTask, identifier)
            activity = TaskActivity(self.sessions, DocumentTask, identifier, task.attempts, self.diagnostics)
            activity("preparing", message="检查文档材料" if task.attempts == 1 else "继续处理，检查可复用材料")
            self.check(identifier)
            current = self.get_settings()
            folder = current.data_dir / "documents" / task.document_id
            material = load_material(folder)
            verify_snapshot(material, task.config["source"])
            check_generation(task.config)
            settings = execution_settings(current, task.config, required_fields=EXECUTION_FIELDS)
            with self.sessions.begin() as session:
                session.get(DocumentTask, identifier).warnings = material.get("warnings", [])
            result, timings, usage = asyncio.run(self.generate(task, settings, folder, material, timings))
            timings["total"] = round(time.monotonic() - start, 3)
            self.succeed(identifier, result, timings, usage)
            self.cleanup_checkpoints(identifier)
        except (DocumentStopped, TaskInterrupted):
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = session.get(DocumentTask, identifier)
                if task and task.status in {"running", "stopping"}:
                    paused = task.status == "stopping" or shutdown is None or not shutdown.is_set()
                    task.status = "paused" if paused else "interrupted"
                    task.error = None if paused else {"code": "interrupted", "message": self.recovery_message}
                    task.updated_at = now()
                    task.timings = {**timings, "total": round(time.monotonic() - start, 3)}
                    in_session(session, task, self.diagnostics)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "document_generation_failed"
            message = exc.message if isinstance(exc, AppError) else "文档笔记生成失败，已保留成功检查点。"
            report = self.diagnostics.record(code, message, exc, task_id=identifier)
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = session.get(DocumentTask, identifier)
                if task and task.status in {"running", "stopping"}:
                    stopping = task.status == "stopping"
                    task.status = "paused" if stopping else "failed"
                    task.error = None if stopping else {"code": code, **report}
                    task.timings = {**timings, "total": round(time.monotonic() - start, 3)}
                    task.updated_at = now()
                    in_session(session, task, self.diagnostics)
        finally:
            timings["total"] = round(time.monotonic() - start, 3)
            try:
                with self.sessions.begin() as session:
                    task = session.get(DocumentTask, identifier)
                    if task:
                        task.timings = dict(timings)
            except Exception as exc:
                self.diagnostics.record("task_timings_save_failed", "执行耗时保存失败，保留任务原有状态和退出原因。", exc, task_id=identifier)
            self.shutdown = None

    def cleanup_checkpoints(self, identifier):
        self.checkpoint_cleanup.cleanup(identifier)

    def retry_checkpoint_cleanup(self):
        self.checkpoint_cleanup.retry()
