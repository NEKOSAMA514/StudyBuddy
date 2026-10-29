"""识别设置与串行下载任务；下载完成不代表识别效果已经验证。"""
from dataclasses import replace
import asyncio
import hashlib
import json
import os
import shutil
import threading
import time
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from ..errors import AppError
from ..util.json_io import atomic_json
# 保留既有调用者使用的目录及准备函数名字；共享原对象，不复制规则。
from .model_catalog import MODELS, option_for, model_path, model_ready, runtime_ready
from .model_files import prepare_bundled, verify_paraformer


class RecognitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asr_option: str = Field(max_length=60)
    ocr_option: str = Field(max_length=60)
    language: Literal["zh", "en", "auto"] = "zh"


class AnalysisInput(RecognitionInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    language: Literal["zh", "en", "auto"] | None = None
    asr_option: str | None = Field(default=None, max_length=60)
    ocr_option: str | None = Field(default=None, max_length=60)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    note_style: Literal["concise", "detailed"] | None = None

    @model_validator(mode="after")
    def one_model_source(self):
        if self.model is not None and self.llm_profile_id is not None:
            raise ValueError("API 配置与型号覆盖不能同时提交")
        return self


# 将本次识别选择应用到候选快照，不改变全局默认识别设置。
def analysis_candidate(settings, value):
    if value.asr_option is not None or value.ocr_option is not None or value.language is not None:
        recognition = RecognitionInput(asr_option=value.asr_option or option_for(settings), ocr_option=value.ocr_option or settings.ocr_engine,
                                       language=value.language or settings.asr_language)
        settings = recognition_candidate(settings, recognition)
    if value.llm_profile_id is not None:
        from ..settings.model_profiles import find, for_profile
        settings = for_profile(settings, find(settings, value.llm_profile_id))
    return replace(settings, llm_model=value.model or settings.llm_model, note_style=value.note_style or settings.note_style)


def recognition_candidate(settings, value):
    for identifier, kind in [(value.asr_option, "asr"), (value.ocr_option, "ocr")]:
        spec = MODELS.get(identifier)
        if not spec or spec["kind"] != kind:
            raise AppError("unknown_model", "请选择列表中的识别模型。")
        if not runtime_ready(identifier):
            raise AppError("runtime_missing", "缺少模型运行引擎，请按说明重新构建项目。")
        if not model_ready(settings, identifier):
            raise AppError("model_not_downloaded", "请先下载或准备所选模型文件，再保存设置。")
    if value.asr_option == "paraformer" and value.language == "en":
        raise AppError("language_unsupported", "当前 Paraformer 权重面向中文，英文请选 Whisper 等模型。")
    spec = MODELS[value.asr_option]
    return replace(settings, asr_engine=spec["engine"], asr_model=spec["model"],
                   asr_language=value.language, ocr_engine=value.ocr_option)


def recognition_settings(settings):
    return {"asr_option": option_for(settings), "ocr_option": settings.ocr_engine, "language": settings.asr_language}


def save_recognition(settings):
    path = settings.data_dir / "_private" / "recognition-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, recognition_settings(settings), ensure_ascii=True)


def load_recognition(settings):
    path = settings.data_dir / "_private" / "recognition-settings.json"
    if not path.exists():
        return settings
    value = RecognitionInput.model_validate_json(path.read_text(encoding="utf-8"))
    # 重启后仍保留选择；文件丢失在实际分析时明确报错，不偷偷切换引擎。
    spec = MODELS.get(value.asr_option)
    if not spec or spec["kind"] != "asr" or value.ocr_option != "rapidocr":
        raise AppError("unknown_model", "已保存的识别模型配置无效。")
    return replace(settings, asr_engine=spec["engine"], asr_model=spec["model"], asr_language=value.language, ocr_engine=value.ocr_option)


# 管理型号目录、断点下载及取消，下载完成不自动切换当前模型。
class ModelDownloads:
    def __init__(self, settings, diagnostics):
        self.settings, self.diagnostics = settings, diagnostics
        self.lock = threading.Lock()
        self.active = None
        self.thread = self.loop = self.transfer = None
        self.command = None
        self.closed = False
        self.states = {}
        self.directory = settings.model_dir / "_downloads"
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["status"] == "cancelling" and path.stem in MODELS and MODELS[path.stem]["kind"] == "asr":
                    self.cleanup_partial(path.stem)
                    value.update(status="cancelled", message="下载已取消。", error=None, downloaded_bytes=0,
                                 total_bytes=None, partial=None, revision=None)
                    atomic_json(path, value)
                elif value["status"] == "pausing":
                    value.update(status="paused", message="下载已暂停，可以继续下载。", error=None)
                    atomic_json(path, value)
                elif value["status"] in {"downloading", "preparing"}:
                    message = "上次下载已中断，可以继续下载。"
                    value.update(status="interrupted", message=message, error={"message": message,
                                 **self.diagnostics.record("download_interrupted", message, model=path.stem)})
                self.states[path.stem] = value
            except (ValueError, KeyError, OSError, AppError) as exc:
                self.diagnostics.record("download_state_invalid", "下载状态文件损坏。", exc, file=path.name)

    def update(self, identifier, **values):
        with self.lock:
            self._update(identifier, **values)

    def _update(self, identifier, **values):
        value = {**self.states.get(identifier, {}), **values}
        self.states[identifier] = value
        atomic_json(self.directory / f"{identifier}.json", value)

    def catalog(self, settings):
        with self.lock:
            states = dict(self.states)
            active = self.active
        return {"settings": recognition_settings(settings), "model_directory": "models/", "active_download": active,
                "models": [{"id": identifier, **{k: spec[k] for k in ("kind", "name", "description", "size", "docs")},
                            "directory": f"models/{spec['directory']}", "ready": model_ready(settings, identifier),
                            "directory_exists": model_path(settings, identifier).is_dir(),
                            "runtime_ready": runtime_ready(identifier), "download": states.get(identifier)} for identifier, spec in MODELS.items()]}

    def model_directory(self, identifier, *, allow_ocr=False):
        spec = MODELS.get(identifier)
        if not spec or (spec["kind"] != "asr" and not allow_ocr):
            raise AppError("unknown_model", "请选择列表中的语音识别模型。", 404)
        path = model_path(self.settings, identifier)
        directory = path.resolve()
        root = self.settings.model_dir.resolve()
        if path.is_symlink() or path.is_junction() or directory == root or directory.parent != root:
            raise AppError("invalid_model_path", "模型目录异常，无法执行文件操作。")
        return directory

    def reveal(self, identifier):
        directory = self.model_directory(identifier, allow_ocr=True)
        if not directory.is_dir():
            raise AppError("file_missing", "模型文件夹不存在，请先下载或准备模型。", 404)
        opener = getattr(os, "startfile", None)
        if opener is None:
            raise AppError("model_folder_unavailable", "打开模型位置需要在 Windows 本机运行 StudyBuddy。")
        try:
            opener(str(directory))
        except OSError as exc:
            raise AppError("model_folder_unavailable", "无法打开模型位置，请检查文件夹是否仍存在。") from exc
        return {"opened": True}

    def delete(self, identifier):
        with self.lock:
            if self.active:
                raise AppError("download_busy", "已有模型正在下载，请等待完成后再删除。", 409)
            directory = self.model_directory(identifier)
            try:
                if directory.exists():
                    shutil.rmtree(directory)
                (self.directory / f"{identifier}.json").unlink(missing_ok=True)
            except OSError as exc:
                raise AppError("model_delete_failed", "模型文件删除失败，文件可能正在被其他程序使用。") from exc
            self.states.pop(identifier, None)
        return {"deleted": True}

    # 检查型号与互斥状态后启动下载，继续操作复用有效的断点文件。
    def start(self, identifier, *, resume=False):
        if identifier not in MODELS:
            raise AppError("unknown_model", "请选择列表中的模型。", 404)
        with self.lock:
            if self.closed:
                raise AppError("download_interrupted", "服务正在关闭，请稍后继续下载。", 503)
            if self.active:
                raise AppError("download_busy", "已有模型正在下载，请等待完成。", 409)
            previous = self.states.get(identifier, {})
            if resume and previous.get("status") not in {"paused", "interrupted"}:
                raise AppError("not_ready", "这个模型没有可继续的下载。", 409)
            self.active = identifier
            self.command = None
            try:
                self._update(identifier, status="downloading", message="正在继续下载…" if resume else "开始准备模型文件…",
                             downloaded_bytes=previous.get("downloaded_bytes", 0) if resume else 0,
                             total_bytes=previous.get("total_bytes") if resume else None, error=None, resume=resume,
                             revision=previous.get("revision") if resume else None,
                             partial=previous.get("partial") if resume else None)
                self.thread = threading.Thread(target=self.run, args=(identifier,), daemon=True, name="model-download")
                self.thread.start()
            except Exception:
                self.active = None
                raise
        return {"id": identifier, "status": "downloading"}

    def cleanup_partial(self, identifier):
        directory = self.model_directory(identifier)
        for name in MODELS[identifier]["files"]:
            path = directory / name
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)

    # 区分暂停、继续和取消，取消只清理未完成文件，不删除已有完整模型。
    def control(self, identifier, action):
        self.model_directory(identifier)
        with self.lock:
            status = self.states.get(identifier, {}).get("status")
            if self.active != identifier:
                if (action, status) in {("cancel", "cancelled"), ("pause", "paused")}:
                    return {"id": identifier, "status": status}
                if action != "cancel" or status not in {"paused", "interrupted"}:
                    raise AppError("not_ready", "这个模型没有可操作的下载。", 409)
                self.cleanup_partial(identifier)
                self._update(identifier, status="cancelled", message="下载已取消。", error=None,
                             downloaded_bytes=0, total_bytes=None, partial=None, revision=None)
                return {"id": identifier, "status": "cancelled"}
            if status not in {"downloading", "preparing", "pausing", "cancelling"}:
                raise AppError("not_ready", "下载已结束，无需再次操作。", 409)
            first = self.command is None
            if self.command != "cancel":
                self.command = action
            status = "cancelling" if self.command == "cancel" else "pausing"
            self._update(identifier, status=status, message="正在取消下载…" if status == "cancelling" else "正在暂停下载…")
            loop, transfer = self.loop, self.transfer
            if first and loop and transfer:
                loop.call_soon_threadsafe(transfer.cancel)
        return {"id": identifier, "status": status}

    def close(self):
        with self.lock:
            self.closed = True
            first = self.active is not None and self.command is None and self.states[self.active]["status"] not in {"succeeded", "failed"}
            if first:
                self.command = "pause"
            loop, transfer, thread = self.loop, self.transfer, self.thread
            if first and loop and transfer:
                loop.call_soon_threadsafe(transfer.cancel)
        if thread and thread.ident is not None and thread is not threading.current_thread():
            thread.join()

    def check_control(self):
        if self.command:
            raise asyncio.CancelledError

    # 从配置镜像下载并校验权重，失败按既定来源回退，完成后才发布就绪状态。
    def run(self, identifier):
        asyncio.run(self._run(identifier))

    async def _run(self, identifier):
        spec = MODELS[identifier]
        with self.lock:
            self.loop, self.transfer = asyncio.get_running_loop(), asyncio.current_task()
        try:
            self.check_control()
            directory = model_path(self.settings, identifier) if spec["kind"] == "ocr" else self.model_directory(identifier)
            if spec["kind"] == "ocr":
                prepare_bundled(self.settings, identifier, force=True)
            else:
                await self.download_repository(identifier, spec, directory)
            if not model_ready(self.settings, identifier):
                raise AppError("model_incomplete", "模型文件不完整，请重新下载。")
            with self.lock:
                self.check_control()
                self._update(identifier, status="succeeded", message="模型文件已准备好，可以保存设置。", error=None, partial=None)
        except (asyncio.CancelledError, Exception) as exc:
            try:
                if isinstance(exc, asyncio.CancelledError) or self.command:
                    with self.lock:
                        if self.command == "cancel":
                            self.cleanup_partial(identifier)
                            self._update(identifier, status="cancelled", message="下载已取消。", error=None,
                                         downloaded_bytes=0, total_bytes=None, partial=None, revision=None)
                        else:
                            partial = self.states.get(identifier, {}).get("partial") or {}
                            saved = self.states.get(identifier, {}).get("downloaded_bytes", 0)
                            if partial.get("file") in spec["files"]:
                                path = self.model_directory(identifier) / partial["file"]
                                temporary = path.with_suffix(path.suffix + ".part")
                                saved = partial["completed_bytes"] + (temporary.stat().st_size if temporary.exists() else 0)
                            self._update(identifier, status="paused", message="下载已暂停，可以继续下载。", error=None,
                                         downloaded_bytes=saved)
                else:
                    raise exc
            except Exception as error:
                failure = error if isinstance(error, AppError) else AppError("model_download_failed", "模型下载失败，请检查网络、磁盘空间后重试。")
                reference = self.diagnostics.record(failure.code, failure.message, error, model=identifier, stage="model_download")
                self.update(identifier, status="failed", message=reference["message"], error=reference)
        finally:
            with self.lock:
                self.active = None
                self.loop = self.transfer = None
                self.command = None

    async def download_repository(self, identifier, spec, directory):
        for endpoint, label in [("https://hf-mirror.com", "HF-Mirror 镜像"), ("https://huggingface.co", "Hugging Face")]:
            self.check_control()
            self.update(identifier, message=f"正在从 {label} 读取模型信息…")
            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    await self.download_from(identifier, spec, directory, endpoint, label, client)
                self.check_control()
                if identifier == "paraformer":
                    verify_paraformer(directory)
                return
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AppError) as exc:
                self.check_control()
                if isinstance(exc, AppError) and exc.code not in {"model_checksum", "model_size_limit", "model_incomplete", "asr_model_invalid"}:
                    raise
                if endpoint == "https://huggingface.co":
                    raise
                self.update(identifier, downloaded_bytes=0, total_bytes=None,
                            message="镜像下载未完成，正在切换到 Hugging Face…")

    def hash_file(self, path, size, sha256):
        digest = hashlib.sha256() if sha256 else hashlib.sha1(f"blob {size}\0".encode())
        if path is not None:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.check_control()
                    digest.update(chunk)
        return digest

    async def download_from(self, identifier, spec, directory, endpoint, label, client):
        # 固定仓库 + 固定提交下载，逐文件校验 Hub 提供的 SHA；不执行模型仓库代码。
        state = self.states.get(identifier, {})
        resuming = state.get("resume", False)
        revision = state.get("revision") if resuming else None
        revision = revision or spec.get("revision", "main")
        response = await client.get(f"{endpoint}/api/models/{spec['repo']}/revision/{revision}",
                             params={"blobs": "true"}, timeout=30, follow_redirects=True)
        response.raise_for_status()
        metadata = response.json()
        revision = metadata["sha"]
        files = {item["rfilename"]: item for item in metadata["siblings"]}
        total = sum(files[name].get("size", 0) for name in spec["files"]) or None
        self.update(identifier, revision=revision, total_bytes=total)
        received, last = 0, 0
        for name in spec["files"]:
            self.check_control()
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".part")
            detail = files[name]
            size = detail["size"]
            if not 0 < size <= 2 * 1024 ** 3:
                raise AppError("model_size_limit", "模型文件大小超出允许范围。")
            expected = detail["lfs"]["sha256"] if detail.get("lfs") else detail["blobId"]
            sha256 = bool(detail.get("lfs"))
            if resuming and path.is_file() and path.stat().st_size == size and self.hash_file(path, size, sha256).hexdigest() == expected:
                received += size
                self.update(identifier, downloaded_bytes=received, message=f"复用已校验文件 {name}…")
                continue
            partial = {"file": name, "revision": revision, "size": size, "checksum": expected, "completed_bytes": received}
            count = temporary.stat().st_size if resuming and self.states[identifier].get("partial") == partial and temporary.is_file() else 0
            if count > size:
                count = 0
            sha = self.hash_file(temporary if count else None, size, sha256)
            if count == size and sha.hexdigest() != expected:
                count, sha = 0, self.hash_file(None, size, sha256)
            self.update(identifier, partial=partial, downloaded_bytes=received + count)
            for _ in range(2 if count else 1):
                if count == size:
                    break
                headers = {"Accept-Encoding": "identity", **({"Range": f"bytes={count}-"} if count else {})}
                async with client.stream("GET", f"{endpoint}/{spec['repo']}/resolve/{revision}/{name}", headers=headers) as download:
                    if count and download.status_code == 416:
                        count, sha = 0, self.hash_file(None, size, sha256)
                        continue
                    if download.is_error:
                        await download.aread()
                    download.raise_for_status()
                    if count and download.status_code == 200:
                        count, sha = 0, self.hash_file(None, size, sha256)
                    elif count and download.headers.get("content-range") != f"bytes {count}-{size - 1}/{size}":
                        raise AppError("model_checksum", "下载源返回的续传范围不正确。")
                    with temporary.open("ab" if count else "wb") as output:
                        async for chunk in download.aiter_bytes(64 * 1024):
                            self.check_control()
                            count += len(chunk)
                            if count > size:
                                raise AppError("model_size_limit", "下载文件大小与模型目录不符。")
                            output.write(chunk)
                            sha.update(chunk)
                            if time.monotonic() - last > 0.5:
                                self.update(identifier, downloaded_bytes=received + count, total_bytes=total, message=f"正在从 {label} 下载 {name}…")
                                last = time.monotonic()
                break
            if count != size or sha.hexdigest() != expected:
                raise AppError("model_checksum", "模型文件校验失败，请重新下载。")
            self.check_control()
            with self.lock:
                self.check_control()
                temporary.replace(path)
                received += count
                self._update(identifier, downloaded_bytes=received, partial=None)
        self.update(identifier, downloaded_bytes=received, total_bytes=received, message="模型文件校验完成。")
