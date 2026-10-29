"""文档请求的流程协调；存储和后台导入通过构造函数注入。"""
from fastapi.responses import FileResponse
from ..errors import AppError


class DocumentService:
    def __init__(self, store, imports, operation_lock, diagnostics):
        self.store, self.imports = store, imports
        self.operation_lock, self.diagnostics = operation_lock, diagnostics

    # 将存储记录与后台提取摘要合并为文件列表，不在查询时触发提取。
    def list(self):
        extractions = self.imports.summaries()
        return [{**row, "extraction": extractions.get(row["id"])} for row in self.store.list()]

    def upload(self, stream, filename, ocr_option=None, defer_extraction=False):
        return self.store.upload(stream, filename, ocr_option, defer_extraction)

    def start_extraction(self, identifier, value):
        return self.imports.start(identifier, value)

    def extraction_status(self, identifier):
        return self.imports.public(identifier)

    def content(self, identifier, offset, limit):
        return self.store.content(identifier, offset, limit)

    def edit(self, identifier, value):
        return self.store.edit(identifier, value)

    def original(self, identifier):
        path, filename = self.store.original(identifier)
        return FileResponse(path, filename=filename, media_type="application/octet-stream")

    # 共用锁内确认导入空闲并删除记录，提交后再清缓存并返回清理警告。
    def delete(self, identifier):
        with self.operation_lock:
            if self.imports.active(identifier):
                raise AppError("document_busy", "此文档正在后台提取，请等待完成后删除。", 409)
            result = self.store.delete_locked(identifier)
            try:
                self.imports.discard(identifier)
            except Exception as exc:
                result["warning"] = self.diagnostics.record("document_cleanup_failed", "文档记录已删除，部分提取缓存未能清理。", exc, document_id=identifier)
            return result
