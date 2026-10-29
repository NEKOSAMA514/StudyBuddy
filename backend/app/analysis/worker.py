"""本地任务队列、重启恢复与待清理检查点调度。"""
import logging
import queue
import threading
import time
from sqlalchemy import select
from ..db import now
from .activity import in_session

logger = logging.getLogger("studybuddy")


class Worker:
    """本地单进程串行工作线程。数据库是状态真源，队列不是持久化消息系统。"""
    def __init__(self, sessions, diagnostics, handlers):
        self.sessions, self.diagnostics = sessions, diagnostics
        self._handlers = dict(handlers)
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self.active = None
        self.thread = threading.Thread(target=self.loop, daemon=True, name="analysis-worker")

    def register(self, kind, table, pipeline):
        """在工作线程启动前注册处理器，也支持注入测试用流程。"""
        if self.thread.is_alive():
            raise RuntimeError("cannot change handlers while the worker is running")
        self._handlers[kind] = table, pipeline

    def cancel(self, kind, task_id):
        self._handlers[kind][1].cancel_model(task_id)

    # 重启时修正未完成状态，并按最近入队时间恢复视频与文档的统一队列。
    def start(self):
        queued = []
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            for kind, (table, pipeline) in self._handlers.items():
                for task in session.scalars(select(table).where(table.status == "stopping")):
                    task.status, task.error, task.updated_at = "paused", None, now()
                    in_session(session, task, self.diagnostics)
                for task in session.scalars(select(table).where(table.status == "running")):
                    task.status = "interrupted"
                    task.error = {"code": "interrupted", "message": pipeline.recovery_message}
                    task.error = {**task.error, **pipeline.diagnostics.record("interrupted", "服务重启，任务已中断。", task_id=task.id, stage=task.stage)}
                    task.updated_at = now()
                    in_session(session, task, self.diagnostics)
                queued.extend((t.updated_at, kind, t.id) for t in session.scalars(select(table).where(table.status == "queued")))
        for _, kind, task_id in sorted(queued):
            self.submit(kind, task_id)
        self.thread.start()

    def submit(self, kind, task_id):
        if self.stopping.is_set():
            return
        # 恢复任务始终排到队尾，即使停止前的旧队列项尚未被消费。
        with self.pending.mutex:
            item = (kind, task_id)
            while item in self.pending.queue:
                self.pending.queue.remove(item)
                self.pending.unfinished_tasks -= 1
        self.pending.put((kind, task_id))

    # 串行执行仍处于排队状态的任务，并定期补清成功任务的检查点。
    def loop(self):
        next_cleanup = 0
        while not self.stopping.is_set():
            if time.monotonic() >= next_cleanup:
                for _, pipeline in self._handlers.values():
                    pipeline.retry_checkpoint_cleanup()
                next_cleanup = time.monotonic() + 60
            try:
                kind, task_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                table, pipeline = self._handlers[kind]
                with self.sessions() as session:
                    task = session.get(table, task_id)
                    eligible = task is not None and task.status == "queued"
                if eligible:
                    with self._dispatch_lock:
                        if self.stopping.is_set():
                            continue
                        self.active = kind, task_id
                    pipeline.run(task_id, shutdown=self.stopping)
            except Exception as exc:
                self.diagnostics.record("worker_failed", "后台工作线程执行失败。", exc, task_id=task_id, kind=kind)
                logger.error("task=%s worker_error; restart to recover", task_id)
            finally:
                with self._dispatch_lock:
                    self.active = None
                self.pending.task_done()

    # 阻止新任务启动，取消当前模型请求并等待工作线程完成收尾。
    def stop(self):
        with self._dispatch_lock:
            self.stopping.set()
            active = self.active
        try:
            if active:
                self.cancel(*active)
        finally:
            if self.thread.ident is not None:
                while self.thread.is_alive():
                    self.thread.join(timeout=5)
                    if self.thread.is_alive():
                        logger.info("worker_shutdown_waiting task=%s; waiting for operation cleanup", active)
