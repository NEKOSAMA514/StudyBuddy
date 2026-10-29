import time
import threading
from dataclasses import replace
import httpx
from sqlalchemy import select
from ..config import Settings
from ..db import Task, ACTIVE_TASK_STATUSES
from ..errors import AppError, model_error_message
from ..diagnostics import Diagnostics
from ..analysis.llm import ModelClient
from ..recognition.extraction import extraction_config
from ..videos.video_reader import VIDEO_EXTENSIONS
from ..documents.extraction import DOCUMENT_EXTENSIONS, MAX_DOCUMENT_BYTES
from .model_settings import ModelSettingsInput, NoteStyleInput, candidate, public_settings
from . import model_profiles
from . import mineru
from .model_store import load_settings, save_settings
from .private_migration import migrate_private_settings
from .network_settings import (NetworkSettingsInput, load_network_settings, save_network_settings,
                               network_candidate, public_network_settings)
from ..recognition.local_models import (RecognitionInput, load_recognition, save_recognition,
                           recognition_candidate, recognition_settings, prepare_bundled)


# 在数据库和后台任务启动前迁移并加载配置，任何读取失败都停止启动。
def load_application_settings(settings=None):
    settings = settings or Settings()
    settings.prepare()
    for loader in (migrate_private_settings, load_settings, load_recognition, load_network_settings, mineru.load):
        try:
            settings = loader(settings)
        except Exception as exc:
            reference = Diagnostics(settings.log_dir, lambda: settings.llm_key).record(
                "settings_load_failed", "已保存的设置无法读取，启动已停止。", exc, loader=loader.__name__)
            location = reference.get("log_file", "日志写入失败，请查看容器启动输出")
            raise AppError("settings_load_failed", f"已保存的设置无法读取，启动已停止。请检查 data/_private。详细日志：{location}。") from exc
    return settings


class SettingsService:
    """保存成功后发布当前设置；业务使用方自行读取快照。"""

    def __init__(self, current_settings, sessions, downloads, diagnostics, operation_lock, release_recognizer):
        self.current_settings = current_settings
        self.release_recognizer = release_recognizer
        self.sessions = sessions
        self.downloads = downloads
        self.diagnostics = diagnostics
        self.operation_lock = operation_lock
        self._write_lock = threading.Lock()

    @property
    def current(self):
        return self.current_settings.read()

    def get_mineru(self):
        return mineru.public(self.current)

    def update_mineru(self, value):
        return self._update(lambda current: mineru.candidate(current, value), mineru.save, mineru.public)

    # 同一写锁内读取最新值、计算并保存，写盘成功后才替换运行快照。
    def _update(self, compute, save, public):
        with self._write_lock:
            updated = compute(self.current)
            save(updated)
            self.current_settings.replace(updated)
            return public(updated)

    def get_recognition(self):
        # 内置 OCR 权重复制后统一从 models 读取。
        current = self.current
        prepare_bundled(current, "rapidocr")
        return self.downloads.catalog(current)

    def update_recognition(self, value: RecognitionInput):
        return self._update(lambda current: recognition_candidate(current, value), save_recognition, recognition_settings)

    def download_model(self, identifier: str, *, resume=False):
        if identifier == "paraformer":
            with self.sessions() as session:
                if session.scalar(select(Task.id).where(Task.status == "running")):
                    raise AppError("model_in_use", "视频正在处理，请等任务完成后再重新下载模型。", 409)
        return self.downloads.start(identifier, resume=resume)

    def control_model_download(self, identifier: str, action: str):
        self.downloads.model_directory(identifier)
        if action == "resume":
            return self.download_model(identifier, resume=True)
        return self.downloads.control(identifier, action)

    def reveal_model(self, identifier: str):
        return self.downloads.reveal(identifier)

    def delete_model(self, identifier: str):
        with self.operation_lock, self._write_lock:
            self.downloads.model_directory(identifier)
            with self.sessions() as session:
                if session.scalar(select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATUSES))):
                    raise AppError("model_in_use", "有视频任务正在排队或处理，请停止任务后再删除语音模型。", 409)
            # 任务已停止，释放复用识别器，避免继续使用被删除的模型或占用其文件。
            self.release_recognizer()
            return self.downloads.delete(identifier)

    def get_network_settings(self):
        return public_network_settings(self.current)

    async def update_network_settings(self, value: NetworkSettingsInput):
        return self._update(lambda current: network_candidate(current, value), save_network_settings, public_network_settings)

    def get_model_settings(self):
        return public_settings(self.current)

    # 使用当前表单或已保存连接读取模型目录，不保存表单，也不调用生成模型。
    def model_options(self, value=None, identifier=None):
        current = model_profiles.preview(self.current, value, identifier) if value is not None else self.current
        if not current.llm_key:
            raise AppError("model_not_configured", "请填写这组 API 的密钥，再读取模型列表。")
        response = None
        try:
            response = httpx.get(current.llm_base_url + "/models", headers={"Authorization": f"Bearer {current.llm_key}"}, timeout=20, follow_redirects=False)
            response.raise_for_status()
            models = response.json()["data"]
            identifiers = sorted({item["id"] for item in models if isinstance(item.get("id"), str) and len(item["id"]) <= 200})
            if not identifiers:
                raise ValueError("empty model list")
            return {"models": identifiers[:200]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            message = "暂时无法读取模型列表，请检查密钥和网络；也可以手动填写模型 ID。"
            if response is not None:
                message = model_error_message(response.text)
            raise AppError("model_list_failed", self.diagnostics.redact(message, (current.llm_key,))) from exc

    def get_model_profiles(self):
        return model_profiles.public_profiles(self.current)

    def update_model_profile(self, value, identifier=None):
        return self._update(lambda current: model_profiles.edit(current, value, identifier), save_settings, model_profiles.public_profiles)

    def activate_model_profile(self, identifier):
        return self._update(lambda current: model_profiles.activate(current, identifier), save_settings, model_profiles.public_profiles)

    def delete_model_profile(self, identifier):
        return self._update(lambda current: model_profiles.remove(current, identifier), save_settings, model_profiles.public_profiles)

    async def update_model_settings(self, value: ModelSettingsInput):
        return self._update(lambda current: candidate(current, value), save_settings, public_settings)

    def get_note_style(self):
        current = self.current
        return {"summary_prompt": current.summary_prompt, "note_style": current.note_style}

    async def update_note_style(self, value: NoteStyleInput):
        return self._update(lambda current: replace(current, summary_prompt=value.summary_prompt, note_style=value.note_style),
                            save_settings, lambda updated: {"summary_prompt": updated.summary_prompt, "note_style": updated.note_style})

    # 用候选连接发送一次小请求验证可用性，不改变已保存设置。
    async def test_model_settings(self, value: ModelSettingsInput, identifier=None, profile=False):
        started = time.monotonic()
        current = model_profiles.preview(self.current, value, identifier) if profile else candidate(self.current, value)
        async with ModelClient(current) as model:
            model.diagnostics = self.diagnostics
            await model.request([{"role": "user", "content": '只输出 JSON：{"ok":true}，无需解释。'}], max_tokens=current.llm_output_tokens["connection_test"])
        return {"ok": True, "elapsed_seconds": round(time.monotonic() - started, 2)}

    def health(self):
        current = self.current
        return {"status": "ok", "model_configured": bool(current.llm_key),
                "free_tier_confirmed": current.free_tier_confirmed,
                "max_upload_bytes": current.max_upload_bytes, "max_duration_seconds": current.max_duration,
                "supported_video_extensions": list(VIDEO_EXTENSIONS),
                "supported_document_extensions": list(DOCUMENT_EXTENSIONS),
                "max_document_bytes": min(MAX_DOCUMENT_BYTES, current.max_upload_bytes),
                "frame_interval_seconds": current.frame_interval,
                "recognition": extraction_config(current)}
