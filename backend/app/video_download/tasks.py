from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from ..errors import AppError
from ..util.json_io import atomic_json
from .metadata import parse_video
from .download import download_video
from .urls import video_url
from ..settings.network_settings import effective_proxy


# 持有单个后台下载和磁盘状态，重启时将未完成记录明确标为中断。
class VideoDownloads:
    def __init__(self, directory, diagnostics, get_settings):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.diagnostics = diagnostics
        self.get_settings = get_settings
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.current = None
        self.locations = {}
        for path in self.directory.glob("*/task.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not re.fullmatch(r"[0-9a-f]{32}", value["id"]) or path.parent.resolve().parent != self.directory:
                    raise ValueError("下载任务编号无效")
                self.locations[value["id"]] = path.parent
                if value["status"] == "running":
                    value.update(status="interrupted", stage="interrupted", progress=None, path=None,
                                 error=diagnostics.record("download_interrupted", "下载因服务重启而中断，请重新下载。"))
                    self._clean(path.parent)
                    self._save(value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                diagnostics.record("video_download_failed", "下载任务状态无法读取。", exc, task=path.parent.name)

    def _save(self, value):
        path = self.locations[value["id"]] / "task.json"
        atomic_json(path, value)

    def _clean(self, directory):
        directory = directory.resolve()
        if directory == self.directory.resolve() or not directory.is_relative_to(self.directory.resolve()):
            raise AppError("video_output_invalid", "未完成文件不在下载任务目录内，已停止清理。")
        for path in directory.iterdir():
            if path.name in {"task.json", "task.tmp"}:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def get(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise AppError("not_found", "下载任务不存在。", 404)
        with self.lock:
            if self.current and self.current["id"] == identifier:
                value = deepcopy(self.current)
            else:
                directory = self.locations.get(identifier)
                if directory is None or not (directory / "task.json").is_file():
                    raise AppError("not_found", "下载任务不存在。", 404)
                value = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        # 使用已有相对路径读取迁移前的下载，不改写历史记录中的容器绝对路径。
        if value["status"] == "succeeded" and value.get("relative_path"):
            relative = Path(value["relative_path"])
            local = (self.directory.parent / relative).resolve()
            if (len(relative.parts) == 3 and relative.parts[0] == "downloads"
                    and local.parent == self.locations[identifier].resolve() and local.is_file()):
                value["path"] = str(local)
        if value["status"] == "succeeded" and value.get("path"):
            local = Path(value["path"]).resolve()
            if local.parent != self.locations[identifier].resolve() or not local.is_file() or not local.stat().st_size:
                value["path"] = None
        # 成品容器和大小以实际文件为准，也纠正旧任务的展示，不改写历史记录。
        if value["status"] == "succeeded" and value.get("path") and value.get("metadata"):
            path = Path(value["path"]).resolve()
            option = next((item for item in value["metadata"].get("quality_options", [])
                           if item["height"] == value.get("quality")), None)
            if option is not None and path.parent == self.locations[identifier].resolve():
                try:
                    size = path.stat().st_size
                except OSError:
                    pass
                else:
                    option.update(ext=path.suffix.lstrip("."), filesize=size, approximate=False)
        return value

    def reveal(self, identifier):
        value = self.get(identifier)
        if value["status"] != "succeeded":
            raise AppError("not_ready", "下载尚未完成，无法打开文件所在位置。", 409)
        if not value.get("path"):
            raise AppError("file_missing", "下载文件不存在或已被移动。", 404)
        opener = getattr(os, "startfile", None)
        if opener is None:
            raise AppError("video_output_invalid", "打开目录需要在 Windows 本机运行 StudyBuddy。")
        opener(str(Path(value["path"]).parent))
        return {"opened": True}

    # 创建任务并在启动时固定代理，后续设置保存不改变进行中的下载。
    def start(self, url, quality):
        url = video_url(url)
        with self.lock:
            if self.stop.is_set():
                raise AppError("download_interrupted", "服务正在关闭，请稍后重试。", 503)
            if self.thread and self.thread.is_alive():
                raise AppError("download_busy", "已有视频正在下载，请等待完成后再下载。", 409)
            identifier = uuid.uuid4().hex
            (self.directory / identifier).mkdir()
            self.locations[identifier] = self.directory / identifier
            self.current = {"id": identifier, "url": url, "quality": quality, "metadata": None,
                            "status": "running", "stage": "parsing", "progress": None, "error": None,
                            "path": None, "relative_path": None, "created_at": datetime.now(timezone.utc).isoformat()}
            self._save(self.current)
            result = deepcopy(self.current)
            self.thread = threading.Thread(target=self._run, args=(effective_proxy(self.get_settings()),), daemon=True, name="video-download")
            self.thread.start()
            return result

    # 接续解析、下载和成品校验，成功后整理目录，失败时清理未完成文件。
    def _run(self, proxy):
        identifier = self.current["id"]
        directory = self.directory / identifier
        last_saved = 0

        def update(changes):
            nonlocal last_saved
            with self.lock:
                stage_changed = changes.get("stage", self.current["stage"]) != self.current["stage"]
                self.current.update(changes)
                if stage_changed or "status" in changes or time.monotonic() - last_saved >= 1:
                    self._save(self.current)
                    last_saved = time.monotonic()

        try:
            source_path = directory / "source.info.json"
            metadata = parse_video(self.current["url"], self.stop, source_path, proxy)
            update({"metadata": metadata, "url": metadata["url"], "stage": "downloading"})
            path = download_video(metadata["url"], self.current["quality"], directory, update, self.stop, source_path, proxy)
            source_path.unlink(missing_ok=True)
            if self.stop.is_set():
                raise AppError("download_interrupted", "下载已中断，请重新下载。")
            target = self.directory / path.stem
            number = 2
            while target.exists():
                target = self.directory / f"{path.stem} ({number})"
                number += 1
            target = target.resolve()
            if directory.resolve().parent != self.directory or target.parent != self.directory or path.parent != directory:
                raise AppError("video_output_invalid", "下载成品不在当前任务目录内，已停止改名。")
            with self.lock:
                directory.rename(target)
                directory = target
                self.locations[identifier] = directory
                path = directory / path.name
                if path.stem != directory.name:
                    path = path.rename(directory / (directory.name + path.suffix))
            update({"status": "succeeded", "stage": "completed", "progress": 100, "path": str(path),
                    "relative_path": f"downloads/{directory.name}/{path.name}"})
        except Exception as exc:
            error = exc if isinstance(exc, AppError) else AppError("video_download_failed", "视频下载失败，请查看错误报告。")
            reference = self.diagnostics.record(error.code, error.message, exc, task_id=identifier, stage=self.current["stage"])
            try:
                self._clean(directory)
            except OSError as cleanup:
                self.diagnostics.record("video_download_failed", "未完成文件清理失败。", cleanup, task_id=identifier)
            try:
                update({"status": "interrupted" if self.stop.is_set() else "failed",
                        "stage": "interrupted" if self.stop.is_set() else "failed",
                        "progress": None, "path": None, "relative_path": None, "error": reference})
            except OSError:
                pass  # 状态已在内存更新；错误报告仍提供日志写入失败信息。

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)
