"""用独立本地线程准备可丢弃的播放副本，不占用分析任务队列。"""
from concurrent.futures import ThreadPoolExecutor, CancelledError
from threading import Event, RLock
from .video_reader import probe, prepare_playback
from ..errors import AppError

PLAYBACK_VERSION = "playback-v1"


# 独立调度播放副本的生成与状态读取，关闭时等待进程清理。
class PlaybackCopies:
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playback")
        self.lock = RLock()
        self.jobs = {}
        self.closed = False

    def output(self, source):
        return source.parent / f"{PLAYBACK_VERSION}.mp4"

    def status(self, identifier, source):
        with self.lock:
            job = self.jobs.get(identifier)
            if job:
                if job["status"] != "ready" or self.output(source).is_file():
                    return {key: job[key] for key in ("status", "error")}
                self.jobs.pop(identifier)
            output = self.output(source)
            if output.is_file():
                try:
                    probe(output)
                except AppError:
                    output.unlink(missing_ok=True)
                else:
                    self.jobs[identifier] = {"status": "ready", "error": None}
                    return {"status": "ready", "error": None}
            return {"status": "unprepared", "error": None}

    def prepare(self, identifier, source, media):
        with self.lock:
            if self.closed:
                raise AppError("not_ready", "服务正在关闭，请稍后重试。", 503)
            state = self.status(identifier, source)
            if state["status"] in {"queued", "preparing", "ready"}:
                return state
            job = {"status": "queued", "error": None, "cancel": Event()}
            self.jobs[identifier] = job
            job["future"] = self.executor.submit(self._run, identifier, source, media, job)
            return {"status": "queued", "error": None}

    def _run(self, identifier, source, media, job):
        try:
            with self.lock:
                job["status"] = "preparing"
            prepare_playback(source, self.output(source), media, job["cancel"])
            with self.lock:
                job["status"] = "ready"
        except Exception as exc:
            if not job["cancel"].is_set():
                reference = self.diagnostics.record(getattr(exc, "code", "internal_error"),
                    getattr(exc, "message", "播放视频准备失败，请重试。"), exc, video_id=identifier, operation="playback")
                with self.lock:
                    job.update(status="failed", error=reference)

    def cancel(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            future = job.get("future") if job else None
            if future:
                job["cancel"].set()
                future.cancel()
        if future:
            try:
                future.result()
            except CancelledError:
                pass
        with self.lock:
            self.jobs.pop(identifier, None)

    def close(self):
        with self.lock:
            self.closed = True
            identifiers = list(self.jobs)
        for identifier in identifiers:
            self.cancel(identifier)
        self.executor.shutdown(wait=True, cancel_futures=True)
