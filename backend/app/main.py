import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .db import database, Task, DocumentTask
from .videos.pipeline import Pipeline
from .analysis.worker import Worker
from .videos.playback import PlaybackCopies
from .recognition.local_models import ModelDownloads
from .settings.network_settings import effective_proxy
from .settings.settings_service import SettingsService, load_application_settings
from .videos.video_service import VideoService
from .videos.analysis_service import AnalysisService
from .videos.evidence_service import EvidenceService
from .http import configure_http
from .routes import settings_router, video_router, analysis_router, evidence_router
from .video_download.tasks import VideoDownloads
from .video_download.api import download_router
from .video_download.bilibili_session import BilibiliSessions, bilibili_router
from .documents.service import DocumentService
from .documents.store import DocumentStore
from .settings.current import CurrentSettings
from .diagnostics import Diagnostics
from .documents.api import document_router
from .documents.pipeline import DocumentPipeline
from .documents.model import DocumentModelClient
from .documents.tasks import DocumentAnalyses
from .documents.imports import DocumentImports
from .documents.api import analysis_router as document_analysis_router
from .note_search import search_router
from .note_marks import mark_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx 的请求日志可能包含供应商 URL；应用日志只保留任务编号及错误码。
logging.getLogger("httpx").setLevel(logging.WARNING)


# 先完成设置迁移和校验，再装配共享资源、两类任务及 HTTP 入口。
def create_app(settings=None, pipeline_factory=Pipeline, start_worker=True, document_model_factory=DocumentModelClient):
    settings = load_application_settings(settings)
    database_path = settings.data_dir / "studybuddy.sqlite3"
    legacy_path = settings.data_dir / "studynote.sqlite3"
    # 旧资料继续使用原数据库及其 WAL，不因更名创建一套空记录。
    if not database_path.exists() and legacy_path.exists():
        database_path = legacy_path
    engine, sessions = database(database_path)
    current = CurrentSettings(settings)
    def current_secrets():
        snapshot = current.read()
        return snapshot.llm_key, snapshot.mineru_token
    # 所有业务共用诊断对象，脱敏时读取当前凭据，避免保存设置后沿用旧值。
    diagnostics = Diagnostics(settings.log_dir, current_secrets)
    pipeline = pipeline_factory(current.read, sessions, diagnostics)
    downloads = ModelDownloads(settings, diagnostics)
    video_downloads = VideoDownloads(settings.data_dir / "downloads", diagnostics, current.read)
    worker = Worker(sessions, diagnostics, {"video": (Task, pipeline)})
    current_settings = SettingsService(current, sessions, downloads, diagnostics, worker.lock, pipeline.release_recognizer)
    document_pipeline = DocumentPipeline(current.read, sessions, diagnostics, document_model_factory)
    worker.register("document", DocumentTask, document_pipeline)
    playback = PlaybackCopies(diagnostics)

    # 启动时恢复后台导入，退出时先等待任务清理，再释放下载和诊断资源。
    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        imports.recover()
        try:
            yield
        finally:
            imports.close()
            if start_worker:
                worker.stop()
            downloads.close()
            playback.close()
            video_downloads.close()
            diagnostics.close()

    app = FastAPI(title="StudyBuddy · 本地视频分析", lifespan=lifespan)
    app.state.current_settings = current
    app.state.settings_service = current_settings
    app.state.sessions = sessions
    app.state.pipeline = pipeline
    app.state.worker = worker
    app.state.diagnostics = diagnostics
    app.state.downloads = downloads
    app.state.video_downloads = video_downloads
    app.state.playback = playback
    app.include_router(download_router(video_downloads))

    bilibili_sessions = BilibiliSessions(settings.data_dir, lambda: effective_proxy(current.read()))
    pipeline.bilibili_sessions = bilibili_sessions
    app.state.bilibili_sessions = bilibili_sessions
    app.include_router(bilibili_router(bilibili_sessions))
    # 删除、播放准备和任务控制继续共用 Worker 持有的同一把锁。
    videos = VideoService(current.read, sessions, playback, video_downloads, diagnostics, worker.lock)
    analyses = AnalysisService(current.read, sessions, worker)
    evidence = EvidenceService(current.read, sessions, analyses)
    configure_http(app, current.read, diagnostics)
    app.include_router(settings_router(current_settings, diagnostics))
    app.include_router(video_router(videos))
    # 存储、导入和请求协调分开装配，并与视频任务共用操作锁。
    store = DocumentStore(current.read, sessions, diagnostics, worker.lock)
    app.state.document_store = store
    app.state.document_pipeline = document_pipeline
    document_analyses = DocumentAnalyses(store, current.read, sessions, worker)
    imports = DocumentImports(store, document_analyses, current.read, sessions, diagnostics, worker.lock)
    documents = DocumentService(store, imports, worker.lock, diagnostics)
    app.state.document_imports = imports
    app.state.document_analyses = document_analyses
    app.include_router(document_router(documents))
    app.include_router(document_analysis_router(document_analyses))
    app.include_router(analysis_router(analyses))
    app.include_router(evidence_router(evidence))
    app.include_router(search_router(sessions))
    app.include_router(mark_router(sessions))

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")
    return app
