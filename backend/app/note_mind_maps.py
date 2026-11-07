"""按笔记版本管理后台导图生成与本地 PNG。"""
import asyncio
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool

from .analysis.checkpoints import mind_map_path
from .analysis.mind_map import note_payload, render_png
from .errors import AppError
from .note_versions import retained_note
from .settings.model_profiles import find, for_profile
from .util.json_io import replace_file


NoteKind = Literal["video", "document"]


class GenerateMindMap(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    llm_profile_id: str = Field(min_length=1, max_length=64)
    replace_existing: StrictBool = False


class MindMaps:
    def __init__(self, get_settings, sessions, diagnostics, model_factory):
        self.get_settings = get_settings
        self.sessions = sessions
        self.diagnostics = diagnostics
        self.model_factory = model_factory
        self.data_dir = get_settings().data_dir.resolve()
        self.active = {}
        self.failures = {}

    def version(self, session, kind, task_id):
        task = retained_note(session, kind, task_id, "仅可为仍保留的完整笔记生成或查看思维导图。")
        root = (self.data_dir / ("videos" if kind == "video" else "documents")).resolve()
        folder = (root / (task.video_id if kind == "video" else task.document_id)).resolve()
        if folder.parent != root:
            raise ValueError("invalid_mind_map_folder")
        return mind_map_path(folder, task.id), task.result

    def read_version(self, kind, task_id):
        with self.sessions() as session:
            return self.version(session, kind, task_id)

    def state(self, key, path):
        try:
            info = path.stat()
            revision = f"{info.st_mtime_ns:x}-{info.st_size:x}"
        except FileNotFoundError:
            revision = None
        error = self.failures.get(key)
        status = "generating" if key in self.active else "failed" if error else "idle"
        return {"status": status, "image_revision": revision, "error": error}

    async def start(self, kind, task_id, value):
        path, note = await run_in_threadpool(self.read_version, kind, task_id)
        key = (kind, task_id)
        state = self.state(key, path)
        if state["status"] == "generating" or (state["image_revision"] and not value.replace_existing):
            return state
        current = self.get_settings()
        settings = for_profile(current, find(current, value.llm_profile_id))
        if not settings.llm_key:
            raise AppError("model_not_configured", "请先在模型设置中为所选 API 配置填写密钥。")
        self.failures.pop(key, None)
        self.active[key] = asyncio.create_task(self.generate(key, settings, note))
        return {**state, "status": "generating", "error": None}

    def save(self, kind, task_id, png):
        with self.sessions.begin() as session:
            # 与资料删除和版本淘汰串行，生成期间被移除的版本不能重新写出文件。
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            path, _ = self.version(session, kind, task_id)
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_suffix(".tmp")
            try:
                temporary.unlink(missing_ok=True)
                with temporary.open("xb") as output:
                    output.write(png)
                replace_file(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    async def generate(self, key, settings, note):
        try:
            model = self.model_factory(settings)
            model.diagnostics = self.diagnostics
            payload = await run_in_threadpool(note_payload, model, note)
            async with model:
                tree = await model.generate("mind_map", payload, lambda _: None)
            png = await run_in_threadpool(render_png, tree)
            # 服务关闭时允许正在进行的原子替换收尾。
            saving = asyncio.create_task(run_in_threadpool(self.save, *key, png))
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                await saving
                raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "mind_map_generation_failed"
            message = exc.message if isinstance(exc, AppError) else "思维导图生成失败，请重试。"
            self.failures[key] = self.diagnostics.record(code, message, exc, kind=key[0], task_id=key[1])
        finally:
            self.active.pop(key, None)

    async def close(self):
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def mind_map_router(maps):
    router = APIRouter(prefix="/api/v1/note-mind-maps")

    @router.get("/{kind}/{task_id}")
    async def status(kind: NoteKind, task_id: str, response: Response):
        path, _ = await run_in_threadpool(maps.read_version, kind, task_id)
        response.headers["Cache-Control"] = "no-store"
        return maps.state((kind, task_id), path)

    @router.post("/{kind}/{task_id}", status_code=202)
    async def generate(kind: NoteKind, task_id: str, value: GenerateMindMap):
        return await maps.start(kind, task_id, value)

    @router.get("/{kind}/{task_id}/image")
    def image(kind: NoteKind, task_id: str, download: bool = False):
        path, note = maps.read_version(kind, task_id)
        try:
            png = path.read_bytes()
        except FileNotFoundError:
            raise AppError("not_found", "这个笔记版本还没有思维导图。", 404) from None
        title = " ".join(note["title"].split())[:80]
        filename = quote(f"{title} 思维导图.png", safe="")
        disposition = "attachment" if download else "inline"
        return Response(png, media_type="image/png", headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
        })

    return router
