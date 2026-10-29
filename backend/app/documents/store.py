import json
import hashlib
import shutil
import uuid
from dataclasses import replace

from sqlalchemy import select

from ..db import Document, DocumentTask, ACTIVE_TASK_STATUSES
from ..errors import AppError
from ..note_marks import clear_marks
from ..util.file_ingest import save_stream
from ..util.file_contracts import FileEdit
from .extraction import DOCUMENT_EXTENSIONS, MAX_DOCUMENT_BYTES, extract
from .task_rules import latest_task
from .image_text import ImageText
from .schemas import public_document
from .formatting import literal
from ..recognition.model_catalog import runtime_ready, model_ready



# 负责文档原件、提取材料与数据库记录的一致性，不承担后台导入调度。
class DocumentStore:
    def __init__(self, get_settings, sessions, diagnostics, lock):
        self.get_settings, self.sessions, self.diagnostics = get_settings, sessions, diagnostics
        self.root = get_settings().data_dir / "documents"
        self.root.mkdir(exist_ok=True)
        self.lock = lock
        self.preparing = set()

    def get(self, session, identifier):
        row = session.get(Document, identifier)
        if row is None:
            raise AppError("document_missing", "文档不存在或已被删除。", 404)
        return row

    # 资料编号必须落在文档根目录的直接子目录，所有文件读写共用此检查。
    def folder(self, identifier):
        folder = (self.root / identifier).resolve()
        if folder.parent != self.root.resolve():
            raise AppError("invalid_document", "文档编号无效。")
        return folder

    def list(self):
        with self.sessions() as session:
            return [{**public_document(row), "latest_task": latest_task(session, row.id)}
                    for row in session.scalars(select(Document).order_by(Document.created_at.desc()))]

    # 先保存原件并查重，耗时提取放在短锁外，登记失败时清理本次目录。
    def upload(self, stream, filename, ocr_option=None, defer_extraction=False, *, _settings=None, _content=None, _identity=None, check_stop=None):
        filename = (filename or "document").replace("\\", "/").split("/")[-1]
        extension = filename.rsplit(".", 1)[-1].lower()
        if extension not in DOCUMENT_EXTENSIONS:
            raise AppError("unsupported_extension", "请选择 PDF、DOCX、PPTX、XLSX、CSV、TXT 或 Markdown 文件。")
        settings = _settings or self.get_settings()
        if ocr_option is not None and not defer_extraction:
            settings = replace(settings, ocr_engine=ocr_option)
            if extension in {"pdf", "docx", "pptx"}:
                if not runtime_ready(ocr_option):
                    raise AppError("runtime_missing", "所选 OCR 模型的运行组件不可用，请在设置中检查。")
                if not model_ready(settings, ocr_option):
                    raise AppError("model_not_downloaded", "所选 OCR 模型文件未准备好，请在设置中检查。")
        identifier = uuid.uuid4().hex
        folder = self.folder(identifier)
        folder.mkdir()
        try:
            saved = save_stream(stream, folder / f"original.{extension}", min(MAX_DOCUMENT_BYTES, settings.max_upload_bytes))
            if defer_extraction:
                saved["fingerprint"] = "pending:" + saved["fingerprint"]
            else:
                saved["fingerprint"] = self.fingerprint(extension, saved["fingerprint"], settings.ocr_engine, _identity)
            # 记录登记、任务创建和删除共用工作线程的短操作锁。
            with self.lock:
                with self.sessions() as session:
                    existing = session.scalar(select(Document).where(Document.fingerprint == saved["fingerprint"]))
                    if existing:
                        shutil.rmtree(folder)
                        return {**public_document(existing), "latest_task": latest_task(session, existing.id), "reused": True}
            # 图片 OCR 在锁外执行，避免阻塞任务取消或其他文件登记。
            content = {"units": [], "character_count": 0, "warnings": []}
            if not defer_extraction:
                images = ImageText(settings, self.diagnostics, check_stop) if check_stop is not None else ImageText(settings, self.diagnostics)
                content = _content if _content is not None else extract(folder / f"original.{extension}", extension, images)
                if extension == "pdf" and _settings is not None and _content is None:
                    for unit in content["units"]:
                        unit.update(text=literal(unit["text"]), format="markdown")
                    content["character_count"] = sum(len(unit["text"]) for unit in content["units"])
                if extension in {"pdf", "docx", "pptx"}:
                    content["ocr_option"] = settings.ocr_engine
                (folder / "content.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            with self.lock:
                with self.sessions.begin() as session:
                    existing = session.scalar(select(Document).where(Document.fingerprint == saved["fingerprint"]))
                    if existing:
                        shutil.rmtree(folder)
                        return {**public_document(existing), "latest_task": latest_task(session, existing.id), "reused": True}
                    row = Document(id=identifier, filename=filename[:200], format=extension,
                                   unit_count=len(content["units"]), character_count=content["character_count"],
                                   warnings=content["warnings"], **saved)
                    session.add(row)
                    session.flush()
                    return {**public_document(row), "reused": False}
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    # 只有可含图片 OCR 的格式合成解析身份，其他格式保持原件哈希。
    @staticmethod
    def fingerprint(extension, original, engine, identity=None):
        if extension not in {"pdf", "docx", "pptx"}:
            return original
        value = f"{original}:{engine}" + (":" + identity if identity else "")
        return hashlib.sha256(value.encode()).hexdigest()

    # 将待处理原件转为正式材料，成功后才移除旧记录并保留原有显示信息。
    def prepare(self, identifier, ocr_option=None, *, _settings=None, _content=None, _identity=None, check_stop=None):
        with self.lock, self.sessions() as session:
            row = self.get(session, identifier)
            if not row.fingerprint.startswith("pending:"):
                return {**public_document(row), "latest_task": latest_task(session, row.id), "reused": True}
            if identifier in self.preparing:
                raise AppError("document_busy", "此文档正在提取，请等待完成后再操作。", 409)
            filename, extension = row.filename, row.format
            self.preparing.add(identifier)
        folder = self.folder(identifier)
        try:
            # 复用上传入口的校验、OCR 快照、去重与回滚。
            # 提取成功前保留待处理原件，失败后仍可重试。
            with (folder / f"original.{extension}").open("rb") as stream:
                upload_name = filename if filename.rsplit(".", 1)[-1].lower() == extension else f"{filename}.{extension}"
                prepared = self.upload(stream, upload_name, ocr_option, _settings=_settings, _content=_content, _identity=_identity, check_stop=check_stop)
            with self.lock, self.sessions.begin() as session:
                row = self.get(session, identifier)
                if not prepared["reused"]:
                    result = self.get(session, prepared["id"])
                    result.filename, result.pinned, result.created_at = row.filename, row.pinned, row.created_at
                    session.flush()
                    prepared = {**public_document(result), "reused": False}
                session.delete(row)
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                prepared["warning"] = self.diagnostics.record("document_cleanup_failed", "文档已提取，待处理副本未能完全清理。", exc, document_id=identifier)
            return prepared
        finally:
            with self.lock:
                self.preparing.discard(identifier)

    def content(self, identifier, offset, limit):
        with self.lock, self.sessions() as session:
            row = self.get(session, identifier)
            if row.fingerprint.startswith("pending:"):
                raise AppError("document_not_prepared", "文档尚未提取，请点击开始处理。", 409)
            try:
                value = json.loads((self.folder(identifier) / "content.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AppError("document_content_missing", "提取内容无法读取，请删除此记录后重新上传原文件。", 409) from exc
            return {"document": public_document(row), "total": row.unit_count,
                    "units": value["units"][offset:offset + limit], "offset": offset}

    def edit(self, identifier, value):
        with self.lock, self.sessions.begin() as session:
            row = self.get(session, identifier)
            for key, item in value.model_dump(exclude_none=True).items():
                setattr(row, key, item)
            session.flush()
            return {**public_document(row), "latest_task": latest_task(session, row.id)}

    def original(self, identifier):
        with self.sessions() as session:
            row = self.get(session, identifier)
            return self.folder(identifier) / f"original.{row.format}", row.filename

    def delete_locked(self, identifier):
        """调用方持有共用操作锁；检查活动导入后提交删除。"""
        folder = self.folder(identifier)
        trash = self.root / f".deleting-{uuid.uuid4().hex}"
        moved = False
        try:
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = self.get(session, identifier)
                if identifier in self.preparing:
                    raise AppError("document_busy", "此文档正在提取，请等待完成后删除。", 409)
                if session.scalar(select(DocumentTask.id).where(DocumentTask.document_id == identifier,
                                                                DocumentTask.status.in_(ACTIVE_TASK_STATUSES))):
                    raise AppError("document_busy", "此文档正在排队或处理中，请先停止任务后删除。", 409)
                # 文件先改名暂存；事务失败可移回，提交后才真正删除目录。
                if folder.exists():
                    folder.rename(trash)
                    moved = True
                clear_marks(session, "document", select(DocumentTask.id).where(DocumentTask.document_id == identifier))
                for task in session.scalars(select(DocumentTask).where(DocumentTask.document_id == identifier)):
                    session.delete(task)
                session.flush()
                session.delete(row)
        except BaseException:
            if moved:
                trash.rename(folder)
            raise
        warning = None
        if moved:
            try:
                shutil.rmtree(trash)
            except OSError as exc:
                warning = self.diagnostics.record("document_cleanup_failed", "文档记录已删除，部分本地文件未能清理。", exc, document_id=identifier)
        return {"deleted": True, "warning": warning}
