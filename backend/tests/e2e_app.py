"""仅用于浏览器测试的工厂：真实上传/数据库/抽帧，预设 ASR/OCR/LLM。

生产工厂不会加载此模块，也没有通过环境变量开启假结果的入口。
"""
from app.config import Settings
from app.main import create_app
from app.videos.pipeline import Pipeline
from app.recognition.extraction import extraction_config, extraction_signature, speech_cache_path, ocr_cache_path
from app.db import Video, Task, Document
from app.documents.model import DocumentModelClient
from app.recognition.local_models import MODELS, model_path
from .fakes import FakeRecognizer, FakeModel
import time
import asyncio
import json
import threading
import uuid
from pathlib import Path


class BrowserRecognizer(FakeRecognizer):
    def ocr(self, image):
        # 60张合计约3秒，留出操作停止按钮的时间；生产没有此延迟。
        time.sleep(0.05)
        return super().ocr(image)


def factory():
    settings = Settings()
    if not (settings.data_dir / ".e2e-isolated").is_file() or not settings.model_dir.is_relative_to(settings.data_dir):
        raise RuntimeError("Use scripts/run-tests.ps1 -Suite Browser with isolated test data.")
    # Catalog placeholders only: fake recognition never loads these as model weights.
    directory = model_path(settings, "paraformer")
    directory.mkdir(parents=True, exist_ok=True)
    for filename in MODELS["paraformer"]["files"]:
        (directory / filename).write_text("BROWSER TEST PLACEHOLDER - NOT A MODEL", encoding="utf-8")
    holds = {}
    class BrowserModel(FakeModel):
        async def generate(self, operation, payload, validate):
            if operation == "chapters" and payload.get("video_title") == "e2e-chemistry-list":
                self.calls += 1
                body = (Path(__file__).parent / "fixtures/chemistry-lists.md").read_text(encoding="utf-8")
                value = {"chapters": [{"chapter_id": chapter["chapter_id"], "summary": "列表内反应式。", "markdown": body, "issues": []}
                                      for chapter in payload["chapters"]]}
                validate(value)
                return value
            held = holds.get(payload.get("video_title"))
            if held and (operation == "outline" and payload["anchors"][0]["start_seconds"] >= 1200 or
                         operation == "chapters" and payload["video_title"].startswith("e2e-outline-")):
                while not held.is_set():
                    await asyncio.sleep(0.02)
            return await super().generate(operation, payload, validate)
    def pipeline(get_settings, sessions, diagnostics):
        return Pipeline(get_settings, sessions, diagnostics, BrowserRecognizer, BrowserModel)
    class BrowserDocumentModel(DocumentModelClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def generate(self, operation, payload, validate):
            self.check_stop()
            self.before_request()
            self.calls += 1
            await asyncio.sleep(0.03)
            if operation == "concise":
                start, end = payload["primary"][0]["id"], payload["primary"][-1]["id"]
                source = start if start == end else f"{start}-{end}"
                value = {"content_type": payload.get("content_type", "课程或教材"), "title": "简洁文档重点",
                         "markdown": f"**主要结论**：概念需要满足材料给出的条件。\n\n## 关键条件 [来源](#source-{source})\n\n- 保留适用范围。\n- 依据原文理解。"}
            elif operation == "outline":
                title = payload.get("document_title", "")
                if title.startswith("e2e-document-held"):
                    while not holds.setdefault(title, threading.Event()).is_set():
                        await asyncio.sleep(0.02)
                anchors = payload["anchors"]
                starts = [anchors[0], anchors[len(anchors) // 2]] if len(anchors) > 1 else [anchors[0]]
                value = {"content_type": payload.get("content_type", "课程或教材"), "document_summary": "模拟文档讨论主要概念与适用条件。", "chapters": [
                    {"title": f"文档主题{i + 1}", "core_question": "核心条件是什么？", "topics": ["概念与条件"], "start_anchor_id": anchor}
                    for i, anchor in enumerate(starts)]}
            elif operation == "chapters":
                value = {"chapters": [{"chapter_id": c["chapter_id"], "summary": "说明概念的适用条件。", "issues": [],
                    "markdown": "**主要结论**：概念需要满足原文给出的条件。\n\n解释概念之间的联系，保留原文边界。"}
                    for c in payload["chapters"]]}
            elif operation == "extract":
                value = {"facts": [{"text": "相关概念与必要条件。"}], "issues": []}
            else:
                value = {"title": "文档主题总结", "tags": ["概念"], "overview_paragraphs": ["模拟生成的文档总结：资料介绍概念及其条件。"]}
            return validate(value)

    app = create_app(settings, pipeline_factory=pipeline, document_model_factory=BrowserDocumentModel)

    # 浏览器测试使用确定的本地替身，任何 PDF 都不会访问真实 MinerU。
    from app.documents import mineru
    from app.errors import AppError
    def fake_mineru(self, pdf, folder, mode, pages):
        from pypdf import PdfReader
        self.check()
        self.report("云端解析", "浏览器测试模拟解析")
        with PdfReader(pdf) as document:
            blocks = [{"type":"text", "text":page.extract_text(), "page_idx":index} for index, page in enumerate(document.pages)]
        return "\n\n".join(block["text"] for block in blocks), blocks if mode == "mineru_vlm" else None
    mineru.MinerUClient.parse = fake_mineru
    import_failures = set()
    create_document = app.state.document_analyses.create
    def create_with_import_failure(identifier, value, **kwargs):
        with app.state.sessions() as session:
            filename = session.get(Document, identifier).filename
        if kwargs and filename in import_failures:
            raise AppError("document_generation_limit", "模拟生成限制，提取内容仍可查看")
        return create_document(identifier, value, **kwargs)
    app.state.document_analyses.create = create_with_import_failure

    @app.post("/__test__/document-import-failure")
    def import_failure(value: dict):
        import_failures.add(value["filename"])
        return {"configured": True}

    @app.post("/__test__/documents/{document_id}/release")
    def release_document(document_id: str):
        with app.state.sessions() as session:
            document = session.get(Document, document_id)
            holds.setdefault(document.filename, threading.Event()).set()
        return {"released": True}

    @app.get("/__test__/document-samples/{extension}")
    def document_sample(extension: str):
        from fastapi.responses import Response
        from .document_samples import sample
        return Response(sample(extension), media_type="application/octet-stream")

    @app.get("/__test__/identity")
    def identity():
        return {"fake_services_used": True}

    @app.post("/__test__/segmented/{video_id}")
    def segmented(video_id: str, preview: bool = False):
        # 在真实Pipeline中停住第二段；只准备本轮隔离识别缓存，不调用真实识别/模型。
        title = ("e2e-outline-" if preview else "e2e-segments-") + video_id
        holds[title] = threading.Event()
        from app.videos.analysis_service import AnalysisService
        config = {**AnalysisService(None, None, None).task_config(settings), "frame_interval": 1200, "execution_started": True,
                  "model": settings.llm_model, "provider": settings.llm_provider, "video_title": title}
        config.pop("frame_count")  # 此分段恢复夹具使用历史固定间隔缓存。
        signature = extraction_signature(config)
        folder = settings.data_dir / "videos" / video_id
        (folder / "audio.wav").write_bytes(b"isolated fake audio")
        speech = [{"start_seconds": seconds, "end_seconds": seconds + 10, "text": "当前片段的讲解。"}
                  for seconds in ((0,) if preview else (0, 1200))]
        speech_cache_path(folder, signature).write_text(json.dumps(speech), encoding="utf-8")
        for index in range(len(speech)):
            ocr_cache_path(folder, signature, index).write_text('{"text":""}', encoding="utf-8")
        task_id = uuid.uuid4().hex
        with app.state.sessions.begin() as session:
            video = session.get(Video, video_id)
            video.duration, video.audio_codec = (120 if preview else 2400), "aac"
            session.add(Task(id=task_id, video_id=video_id, config=config))
        app.state.worker.submit("video", task_id)
        return {"task_id": task_id}

    @app.post("/__test__/segmented/{video_id}/release")
    def release(video_id: str, preview: bool = False):
        holds[("e2e-outline-" if preview else "e2e-segments-") + video_id].set()
        return {"released": True}

    # create_app already mounted the frontend at '/'; match test-only routes first.
    for _ in range(6):
        app.router.routes.insert(0, app.router.routes.pop())
    return app
