"""文档上传与分析的 HTTP 路由。"""
from typing import Literal
from urllib.parse import quote
from fastapi import APIRouter, Form, Query, UploadFile
from fastapi.responses import Response
from ..util.file_contracts import FileEdit
from .schemas import DocumentAnalysisInput, DocumentExtractionInput


# 将上传、提取与资料管理请求交给协调层，路由只处理 HTTP 参数。
def document_router(service):
    router = APIRouter(prefix="/api/v1/documents")

    @router.get("")
    def list_documents():
        return service.list()

    @router.post("", status_code=201)
    def upload_document(file: UploadFile, ocr_option: Literal["rapidocr"] | None = Form(None),
                        defer_extraction: bool = Form(False)):
        try:
            return service.upload(file.file, file.filename, ocr_option, defer_extraction)
        finally:
            file.file.close()

    @router.post("/{identifier}/extract", status_code=202)
    def extract_document(identifier: str, value: DocumentExtractionInput | None = None):
        return service.start_extraction(identifier, value or DocumentExtractionInput())

    @router.get("/{identifier}/extraction")
    def extraction_status(identifier: str):
        from ..errors import AppError
        result = service.extraction_status(identifier)
        if result is None:
            raise AppError("document_missing", "文档提取记录不存在或资料已被删除。", 404)
        return result

    @router.get("/{identifier}/content")
    def document_content(identifier: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        return service.content(identifier, offset, limit)

    @router.get("/{identifier}/original")
    def original_document(identifier: str):
        return service.original(identifier)

    @router.patch("/{identifier}")
    def edit_document(identifier: str, value: FileEdit):
        return service.edit(identifier, value)

    @router.delete("/{identifier}")
    def delete_document(identifier: str):
        return service.delete(identifier)

    return router


# 连接文档笔记的预检、创建、控制和正文读取接口。
def analysis_router(service):
    router = APIRouter(prefix="/api/v1")

    @router.get("/documents/{identifier}/analysis-preview")
    def analysis_preview(identifier: str, llm_profile_id: str | None = None, note_style: Literal["concise", "detailed"] | None = None):
        return service.analysis_preview(identifier, llm_profile_id, note_style)

    @router.post("/documents/{identifier}/analyses", status_code=202)
    def create(identifier: str, value: DocumentAnalysisInput | None = None):
        return service.create(identifier, value)

    @router.get("/documents/{identifier}/analyses")
    def versions(identifier: str):
        return service.notes.versions(identifier)

    @router.get("/document-tasks/{identifier}")
    def status(identifier: str):
        return service.status(identifier)

    @router.post("/document-tasks/{identifier}/{action}", status_code=202)
    def control(identifier: str, action: Literal["stop", "resume", "retry"]):
        return service.control(identifier, action)

    @router.get("/document-analyses/{identifier}")
    def analysis(identifier: str):
        return service.notes.analysis(identifier)

    @router.get("/document-analyses/{identifier}/notes")
    def notes(identifier: str, format: Literal["txt", "md"] = "txt"):
        text, filename = service.notes.download(identifier, format)
        return Response(text, media_type="text/markdown" if format == "md" else "text/plain",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    return router
