"""已确认的文档导入：单文件后台提取、批次恢复和一次性笔记提交。"""
import json
import logging
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from sqlalchemy import select

from ..db import Document, DocumentTask
from ..errors import AppError, TaskInterrupted
from ..settings.model_profiles import for_profile, find, task_model_config, execution_settings
from ..util.json_io import atomic_json
from .extraction import EXTRACTION_VERSION, MAX_TEXT, MAX_UNITS
from .pdf import PdfDocument, page_images, image_entries
from .image_text import ImageText
from .mineru import MinerUClient, MODES, page_markdown, supplement, read_json
from ..recognition.model_catalog import model_ready, runtime_ready

ACTIVE = {"queued", "running"}
BATCH_LIMITS = {"mineru_agent": (20, 10 * 1024 * 1024), "mineru_vlm": (200, 200 * 1024 * 1024)}


class DocumentImports:
    def __init__(self, documents, analyses, get_settings, sessions, diagnostics, operation_lock):
        self.documents, self.analyses = documents, analyses
        self.get_settings, self.sessions = get_settings, sessions
        self.diagnostics, self.operation_lock = diagnostics, operation_lock
        self.root = get_settings().data_dir / "_document_imports"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-import")

    def folder(self, identifier):
        self.documents.folder(identifier)  # 与资料接口同样的路径检查。
        return self.root / identifier

    def state(self, identifier):
        with self.lock:
            return read_json(self.folder(identifier) / "state.json")

    def write(self, identifier, state):
        with self.lock:
            folder = self.folder(identifier)
            folder.mkdir(exist_ok=True)
            atomic_json(folder / "state.json", state)

    def active(self, identifier):
        value = self.summaries().get(identifier)
        return bool(value and value["status"] in ACTIVE)

    def public(self, identifier):
        state = self.state(identifier)
        if not state:
            return None
        # 已删除的资料不能借历史导入记录重新打开。
        with self.sessions() as session:
            if session.get(Document, state.get("document_id") or identifier) is None:
                return None
        return {"source_id": identifier, **{key: state.get(key) for key in ("status", "parser", "progress", "error", "document_id", "task_id")}}

    def summaries(self):
        # 一次读取每份导入状态，不能在资料列表中按每行反复扫描全部文件。
        result = {}
        with self.lock:
            paths = sorted(self.root.glob("*/state.json"), key=lambda path: path.stat().st_mtime_ns)
            for path in paths:
                state = read_json(path)
                target = state.get("document_id") or path.parent.name
                result[target] = {"source_id": path.parent.name, **{key: state.get(key) for key in
                    ("status", "parser", "progress", "error", "document_id", "task_id")}}
        return result

    def report(self, identifier, branch, stage, message):
        with self.lock:
            state = self.state(identifier)
            state["progress"][branch] = {"stage": stage, "message": message}
            self.write(identifier, state)

    # 首次确认保存执行快照和预分配任务编号，重试沿用原配置及云端进度。
    def start(self, identifier, value):
        with self.operation_lock, self.lock:
            if self.stopping.is_set():
                raise AppError("document_busy", "服务正在关闭，请稍后重试。", 409)
            previous = self.state(identifier)
            if previous and previous["status"] in ACTIVE | {"done"}:
                return self.public(identifier)
            current = self.get_settings()
            with self.sessions() as session:
                row = self.documents.get(session, (previous.get("document_id") or identifier) if previous else identifier)
                extension, original = row.format, row.fingerprint
                if not previous and not original.startswith("pending:"):
                    raise AppError("document_already_prepared", "文档已经提取，请沿用已有材料生成笔记。", 409)
            mode = value.parser or (("mineru_vlm" if current.mineru_token else "mineru_agent") if extension == "pdf" else "local")
            if previous and previous.get("document_id") and previous["parser"] != mode:
                raise AppError("document_not_prepared", "材料已经提取，请重新导入原文件以更换解析方式。", 409)
            if extension != "pdf" and mode != "local":
                raise AppError("invalid_input", "目前只有 PDF 支持 MinerU 云端解析。")
            if mode == "mineru_vlm" and not current.mineru_token:
                raise AppError("mineru_token_missing", "请先在 API 设置中填写 MinerU Token。")
            engine = previous["ocr"]["ocr_engine"] if previous else value.ocr_option or current.ocr_engine
            if extension in {"pdf", "docx", "pptx"}:
                if not runtime_ready(engine):
                    raise AppError("runtime_missing", "所选 OCR 模型的运行组件不可用，请在设置中检查。")
                if not model_ready(current, engine):
                    raise AppError("model_not_downloaded", "所选 OCR 模型文件未准备好，请在设置中检查。")
            if previous and previous["parser"] == mode and previous["status"] != "done":
                state = previous
                state.update(status="queued", error=None)
                # 只清除明确失败的云端请求；成功或状态未明的请求继续查询。
                for path in (self.folder(identifier) / state["run_id"]).glob("*/remote.json"):
                    if read_json(path, {}).get("failed"):
                        path.unlink()
            else:
                settings = for_profile(current, find(current, value.llm_profile_id)) if value.llm_profile_id else current
                settings = replace(settings, note_style=value.note_style or settings.note_style)
                if not settings.llm_key:
                    raise AppError("model_not_configured", "请先在设置中填写所选 API 配置的密钥。")
                ocr = {key: getattr(current, key) for key in ("ocr_engine", "ocr_languages", "ocr_min_confidence", "asr_threads")}
                ocr["ocr_engine"] = value.ocr_option or current.ocr_engine
                state = {"status": "queued", "parser": mode, "extension": extension, "original": original,
                         "model": previous["model"] if previous else task_model_config(settings),
                         "ocr": previous["ocr"] if previous else ocr, "run_id": uuid.uuid4().hex,
                         "task_id": previous["task_id"] if previous else uuid.uuid4().hex,
                         "document_id": None, "error": None, "progress": {}}
            self.write(identifier, state)
            self.executor.submit(self.run, identifier)
            return self.public(identifier)

    # 服务启动后重新排入未完成导入，正式材料和已提交任务由执行时查重复用。
    def recover(self):
        for path in self.root.glob("*/state.json"):
            state = read_json(path)
            if state["status"] in ACTIVE or state["status"] == "interrupted":
                state["status"] = "queued"
                self.write(path.parent.name, state)
                self.executor.submit(self.run, path.parent.name)

    def check(self):
        if self.stopping.is_set():
            raise TaskInterrupted()

    def wait(self, seconds):
        if self.stopping.wait(seconds):
            raise TaskInterrupted()

    # 按持久化状态接续提取、保存材料和提交笔记，交接窗口中断也不重复建任务。
    def run(self, identifier):
        try:
            self.check()
            state = self.state(identifier)
            state["status"] = "running"
            self.write(identifier, state)
            # 数据库提交成功、导入状态尚未落盘时重启，不需要再次解析配置或创建任务。
            with self.sessions() as session:
                submitted = session.get(DocumentTask, state["task_id"])
                already_submitted = submitted is not None and submitted.document_id == state["document_id"]
            if already_submitted:
                state.update(status="done", error=None)
                self.write(identifier, state)
                return
            settings = execution_settings(self.get_settings(), state["model"])
            settings = replace(settings, **state["ocr"])
            mode = state["parser"]
            identity = json.dumps({"parser": mode, "is_ocr": mode == "mineru_vlm", "ocr": state["ocr"],
                                   "version": EXTRACTION_VERSION}, sort_keys=True) if mode in MODES else None
            content = None
            if not state["document_id"]:
                # 相同原件、解析参数和 OCR 已有完整材料时，不再请求云端。
                fingerprint = self.documents.fingerprint(state["extension"], state["original"].removeprefix("pending:"), settings.ocr_engine, identity)
                with self.sessions() as session:
                    existing = session.scalar(select(Document.id).where(Document.fingerprint == fingerprint))
                    pending_exists = session.get(Document, identifier) is not None
                if mode in MODES and not existing:
                    content = self.cloud(identifier, state, settings)
                self.check()
                self.report(identifier, "merge", "保存材料", "正在保存完整提取内容")
                if not pending_exists and existing:
                    prepared = {"id": existing}
                else:
                    prepared = self.documents.prepare(identifier, state["ocr"]["ocr_engine"], _settings=settings,
                        _content=content if not existing else {"units": [], "warnings": [], "character_count": 0}, _identity=identity, check_stop=self.check)
                with self.lock:
                    state = self.state(identifier)
                    state["document_id"] = prepared["id"]
                    self.write(identifier, state)
                self.report(identifier, "merge", "材料已保存", "完整提取内容已保存到本机")
            self.check()
            self.report(identifier, "notes", "检查并生成笔记", "材料已就绪，正在执行现有预检")
            result = self.analyses.create(state["document_id"], None, task_id=state["task_id"], settings=settings)
            self.report(identifier, "notes", "已提交笔记任务", "按确认配置继续生成笔记")
            with self.lock:
                state = self.state(identifier)
                state.update(status="done", task_id=result["task_id"], error=None)
                self.write(identifier, state)
        except TaskInterrupted:
            with self.lock:
                state = self.state(identifier)
                state["status"] = "interrupted"
                self.write(identifier, state)
        except Exception as exc:
            reference = self.diagnostics.record("document_import_failed", "文档提取或笔记提交失败。", exc,
                                                         document_id=identifier)
            if isinstance(exc, AppError):
                reference["message"] = self.diagnostics.redact(exc.message)
            with self.lock:
                state = self.state(identifier)
                state.update(status="failed", error=reference)
                self.write(identifier, state)

    # 合并云端正文与对应页的图片 OCR，完成结果落盘后供重启直接复用。
    def cloud(self, identifier, state, settings):
        folder = self.folder(identifier) / state["run_id"]
        folder.mkdir(exist_ok=True)
        cached = read_json(folder / "content.json")
        if cached:
            self.report(identifier, "merge", "复用结果", "复用已合并的完整材料")
            return cached
        source = self.documents.folder(identifier) / "original.pdf"
        # OCR 与网络等待并行；识别器始终留在自身执行线程。
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="document-image-ocr") as executor:
            ocr = executor.submit(self.images, source, folder, settings, identifier)
            results = self.cloud_batches(source, folder, state["parser"], settings.mineru_token, identifier)
            records, warnings = ocr.result()
        self.check()
        units = []
        for batch, text, blocks in results:
            first, last = batch["first"], batch["last"]
            if blocks is None:
                text = supplement(text, [r for r in records if first <= r["page"] <= last])
                units.append({"label": f"第 {first}–{last} 页" if first != last else f"第 {first} 页",
                              "locator": {"page_start": first, "page_end": last}, "text": text})
            else:
                for page in range(first, last + 1):
                    body = page_markdown([b for b in blocks if b["page_idx"] == page - first])
                    units.append({"label": f"第 {page} 页", "locator": {"page": page},
                                  "text": supplement(body, [r for r in records if r["page"] == page])})
        if sum(len(u["text"]) for u in units) > MAX_TEXT:
            raise AppError("document_limit", "文档内容超过500万字符，请拆分后上传。")
        content = {"version": EXTRACTION_VERSION, "units": [{**unit, "id": f"D{i}", "format": "markdown"} for i, unit in enumerate(units, 1)],
                   "warnings": warnings, "character_count": sum(len(u["text"]) for u in units)}
        self.report(identifier, "merge", "合并材料", "云端正文与对应页面图片文字已完成")
        atomic_json(folder / "content.json", content)
        return content

    # 同时按页数和上传大小拆分 PDF，每批保留远端状态以避免重复上传。
    def cloud_batches(self, source, folder, mode, token, identifier):
        results = []
        limit, size = BATCH_LIMITS[mode]
        with PdfDocument(source, check=self.check, max_pages=MAX_UNITS) as document:
            first = 0
            while first < document.page_count:
                self.check()
                last = min(first + limit, document.page_count)
                batch_folder = folder / f"{first + 1}"
                batch_folder.mkdir(exist_ok=True)
                batch = read_json(batch_folder / "batch.json")
                if not batch:
                    while True:
                        raw = document.write_pages(first, last)
                        if len(raw) <= size:
                            break
                        if last == first + 1:
                            raise AppError("document_limit", "PDF 单页超过解析接口大小限制，请选择本地提取或压缩原文件。")
                        last = first + max(1, (last - first) // 2)
                    temporary = batch_folder / "batch.pdf.part"
                    temporary.write_bytes(raw)
                    temporary.replace(batch_folder / "batch.pdf")
                    batch = {"first": first + 1, "last": last}
                    atomic_json(batch_folder / "batch.json", batch)
                else:
                    last = batch["last"]
                report = lambda stage, message: self.report(identifier, "cloud", stage, f"第 {first + 1}–{last} 页 · {message}")
                client = MinerUClient(token, self.check, self.wait, report)
                text, blocks = client.parse(batch_folder / "batch.pdf", batch_folder, mode, last - first)
                results.append((batch, text, blocks))
                first = last
        self.report(identifier, "cloud", "云端解析完成", "所有批次的官方结果已保存")
        return results

    # 逐页保存 OCR 进度和警告，中断恢复时只处理尚未完成的页面。
    def images(self, source, folder, settings, identifier):
        saved = read_json(folder / "ocr.json", {"pages": 0, "records": [], "cache": {}, "warnings": []})
        images = ImageText(settings, self.diagnostics, self.check)
        images.cache, images.warnings = saved["cache"], saved["warnings"]
        with PdfDocument(source, check=self.check, max_pages=MAX_UNITS) as document:
            cache_keys = {}
            if 0 < saved["pages"] < document.page_count:
                # 编码变化不使旧识别失效；映射只存在内存中，不增加缓存条目或改写旧摘要。
                completed = {(r["page"], r["image"]): r["digest"] for r in saved["records"]}
                for index in sorted({r["page"] - 1 for r in saved["records"]}):
                    page = document.read_page(index, text=False)
                    for number, raw, digest in image_entries(page):
                        key = completed.get((index + 1, number))
                        if raw is not None and key in images.cache:
                            cache_keys.setdefault(digest, key)
            for index in range(saved["pages"], document.page_count):
                self.check()
                page = document.read_page(index, text=False)
                self.report(identifier, "ocr", "本地图片 OCR", f"第 {index + 1}/{document.page_count} 页")
                images.warnings.extend(page["warnings"])
                _, records = page_images(page, images, cache_keys)
                saved["records"].extend(records)
                saved.update(pages=index + 1, cache=images.cache, warnings=images.warnings)
                atomic_json(folder / "ocr.json", saved)
                self.check()
        self.report(identifier, "ocr", "图片识别完成", "已保存图片文字及识别警告")
        return saved["records"], saved["warnings"]

    def close(self):
        self.stopping.set()
        logging.getLogger("studybuddy").info("document_import_shutdown_waiting; waiting for OCR safe point and saved state")
        self.executor.shutdown(wait=True)

    def discard(self, identifier):
        """资料删除提交后清理其提取副本；不扫描正式资料或其他目录。"""
        with self.lock:
            for path in self.root.glob("*/state.json"):
                state = read_json(path)
                if path.parent.name != identifier and state.get("document_id") != identifier:
                    continue
                target = path.parent.resolve()
                if target.parent != self.root.resolve() or path.parent.is_symlink() or path.parent.is_junction():
                    raise AppError("invalid_document", "提取缓存目录不安全，未清理该路径。")
                shutil.rmtree(target)
