from typing import Literal, Annotated
from urllib.parse import quote
from fastapi import APIRouter, UploadFile, Query
from fastapi.responses import FileResponse, Response
from .diagnostics import ClientErrorInput
from .recognition.local_models import RecognitionInput, AnalysisInput
from .settings.model_settings import ModelSettingsInput, NoteStyleInput
from .settings.model_profiles import ProfileInput, ModelOptionsInput
from .settings.network_settings import NetworkSettingsInput
from .util.file_contracts import FileEdit
from .api_schemas import TaskPublic


# 将设置管理接口连接到设置服务，不在路由中直接修改流程内部状态。
def settings_router(settings, diagnostics):
    from .settings.mineru import MinerUInput
    router = APIRouter()

    @router.get("/api/v1/settings/mineru")
    def get_mineru():
        return settings.get_mineru()

    @router.put("/api/v1/settings/mineru")
    def update_mineru(value: MinerUInput):
        return settings.update_mineru(value)

    @router.post("/api/v1/diagnostics/client-errors", status_code=201)
    def client_error(value: ClientErrorInput):
        return diagnostics.record(value.code, value.message, stack=value.stack, location=value.location,
                                  request_url=value.request_url, request_method=value.request_method, http_status=value.http_status,
                                  response_body=value.response_body)

    @router.get("/api/v1/settings/recognition")
    def get_recognition():
        return settings.get_recognition()

    @router.put("/api/v1/settings/recognition")
    def update_recognition(value: RecognitionInput):
        return settings.update_recognition(value)

    @router.post("/api/v1/settings/models/{identifier}/download", status_code=202)
    def download_model(identifier: str):
        return settings.download_model(identifier)

    @router.post("/api/v1/settings/models/{identifier}/download/{action}", status_code=202)
    def control_model_download(identifier: str, action: Literal["pause", "resume", "cancel"]):
        return settings.control_model_download(identifier, action)

    @router.post("/api/v1/settings/models/{identifier}/reveal")
    def reveal_model(identifier: str):
        return settings.reveal_model(identifier)

    @router.delete("/api/v1/settings/models/{identifier}")
    def delete_model(identifier: str):
        return settings.delete_model(identifier)

    @router.get("/api/v1/settings/network")
    def get_network_settings():
        return settings.get_network_settings()

    @router.put("/api/v1/settings/network")
    async def update_network_settings(value: NetworkSettingsInput):
        return await settings.update_network_settings(value)

    @router.get("/api/v1/settings/model")
    def get_model_settings():
        return settings.get_model_settings()

    @router.get("/api/v1/settings/model/profiles")
    def get_model_profiles():
        return settings.get_model_profiles()

    @router.post("/api/v1/settings/model/profiles", status_code=201)
    async def add_model_profile(value: ProfileInput):
        return settings.update_model_profile(value)

    @router.post("/api/v1/settings/model/profiles/options")
    def profile_options(value: ModelOptionsInput, profile_id: str | None = None):
        return settings.model_options(value, profile_id)

    @router.post("/api/v1/settings/model/profiles/test")
    async def profile_test(value: ProfileInput, profile_id: str | None = None):
        return await settings.test_model_settings(value, profile_id, profile=True)

    @router.put("/api/v1/settings/model/profiles/{identifier}")
    async def edit_model_profile(identifier: str, value: ProfileInput):
        return settings.update_model_profile(value, identifier)

    @router.post("/api/v1/settings/model/profiles/{identifier}/activate")
    async def activate_model_profile(identifier: str):
        return settings.activate_model_profile(identifier)

    @router.delete("/api/v1/settings/model/profiles/{identifier}")
    async def delete_model_profile(identifier: str):
        return settings.delete_model_profile(identifier)

    @router.get("/api/v1/settings/model/options")
    def model_options():
        return settings.model_options()

    @router.put("/api/v1/settings/model")
    async def update_model_settings(value: ModelSettingsInput):
        return await settings.update_model_settings(value)

    @router.post("/api/v1/settings/model/test")
    async def test_model_settings(value: ModelSettingsInput):
        return await settings.test_model_settings(value)

    @router.get("/api/v1/settings/note-style")
    def get_note_style():
        return settings.get_note_style()

    @router.put("/api/v1/settings/note-style")
    async def update_note_style(value: NoteStyleInput):
        return await settings.update_note_style(value)

    @router.get("/api/v1/health")
    def health():
        return settings.health()

    return router


# 暴露视频文件与播放接口，上传登记和删除事务由视频服务负责。
def video_router(videos):
    router = APIRouter()

    @router.get("/api/v1/videos")
    def list_videos():
        return videos.list_videos()

    @router.patch("/api/v1/videos/{video_id}")
    def edit_video(video_id: str, value: FileEdit):
        return videos.edit_video(video_id, value)

    @router.delete("/api/v1/videos/{video_id}")
    def delete_video(video_id: str):
        return videos.delete_video(video_id)

    @router.post("/api/v1/videos", status_code=201)
    def upload_video(file: UploadFile):
        try:
            return videos.register_video(file.file, file.filename)
        finally:
            file.file.close()

    @router.post("/api/v1/video-downloads/{identifier}/import", status_code=201)
    def import_download(identifier: str):
        return videos.import_download(identifier)

    @router.get("/api/v1/videos/{video_id}/content")
    def video_content(video_id: str):
        source, mime_type = videos.video_content(video_id)
        return FileResponse(source, media_type=mime_type, headers={"Cache-Control": "private, no-cache"})

    @router.get("/api/v1/videos/{video_id}/playback")
    def get_playback(video_id: str):
        return videos.get_playback(video_id)

    @router.post("/api/v1/videos/{video_id}/playback", status_code=202)
    def start_playback(video_id: str):
        return videos.start_playback(video_id)

    @router.get("/api/v1/videos/{video_id}/playback/content")
    def playback_content(video_id: str):
        return FileResponse(videos.playback_content(video_id), media_type="video/mp4",
                            headers={"Cache-Control": "private, no-cache"})

    return router


# 暴露视频任务提交、状态及停止恢复接口，提交只返回任务回执。
def analysis_router(analyses):
    router = APIRouter()

    @router.get("/api/v1/videos/{video_id}/analysis-preview")
    def analysis_preview(video_id: str, value: Annotated[AnalysisInput, Query()]):
        return analyses.analysis_preview(video_id, value)

    @router.get("/api/v1/videos/{video_id}/analyses", response_model=list[TaskPublic])
    def analysis_versions(video_id: str):
        return analyses.versions(video_id)

    @router.post("/api/v1/videos/{video_id}/analyses", status_code=202)
    def analyze(video_id: str, value: AnalysisInput | None = None):
        return analyses.analyze(video_id, value)

    @router.get("/api/v1/tasks/{task_id}")
    def task_status(task_id: str):
        return analyses.task_status(task_id)

    @router.post("/api/v1/tasks/{task_id}/stop", status_code=202)
    def stop_task(task_id: str):
        return analyses.stop_task(task_id)

    @router.post("/api/v1/tasks/{task_id}/resume", status_code=202)
    def resume_task(task_id: str):
        return analyses.resume_task(task_id)

    @router.post("/api/v1/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str):
        return analyses.retry(task_id)

    @router.get("/api/v1/analyses/{analysis_id}")
    def analysis(analysis_id: str):
        return analyses.analysis(analysis_id)

    return router


# 提供依据、转录和笔记下载，只读取服务已经保存的结果。
def evidence_router(evidence_service):
    router = APIRouter()

    @router.get("/api/v1/videos/{video_id}/evidence")
    def video_evidence(video_id: str, task_id: str | None = None):
        return evidence_service.video_evidence(video_id, task_id)

    @router.get("/api/v1/videos/{video_id}/transcript")
    def transcript(video_id: str, task_id: str, format: Literal["txt", "json"] = "txt"):
        content, filename = evidence_service.transcript(video_id, task_id, format)
        return Response(content, media_type="application/json" if format == "json" else "text/plain",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @router.get("/api/v1/analyses/{analysis_id}/notes")
    def download_notes(analysis_id: str, format: Literal["txt", "md"] = "txt"):
        content, filename = evidence_service.download_notes(analysis_id, format)
        return Response(content, media_type="text/markdown" if format == "md" else "text/plain",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @router.get("/api/v1/evidence/{evidence_id}")
    def evidence(evidence_id: str):
        return evidence_service.evidence(evidence_id)

    @router.get("/api/v1/evidence/{evidence_id}/image")
    def evidence_image(evidence_id: str):
        return FileResponse(evidence_service.evidence_image(evidence_id), media_type="image/jpeg")

    return router
