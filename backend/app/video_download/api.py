from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from .metadata import parse_video
from ..settings.network_settings import effective_proxy


class VideoLink(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(min_length=1, max_length=4096)


class DownloadInput(VideoLink):
    quality: int = Field(default=360, ge=1)


# 连接链接解析、下载状态与目录打开接口，后台工作由下载管理器持有。
def download_router(downloads):
    router = APIRouter(prefix="/api/v1/video-downloads", tags=["video-downloads"])

    @router.post("/parse")
    def parse(value: VideoLink):
        return parse_video(value.url, downloads.stop, proxy=effective_proxy(downloads.get_settings()))

    @router.post("", status_code=202)
    def start(value: DownloadInput):
        return downloads.start(value.url, value.quality)

    @router.get("/{identifier}")
    def status(identifier: str):
        return downloads.get(identifier)

    @router.post("/{identifier}/reveal")
    def reveal(identifier: str):
        return downloads.reveal(identifier)

    return router
