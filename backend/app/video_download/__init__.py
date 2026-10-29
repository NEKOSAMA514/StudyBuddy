"""独立的视频下载；不导入文件管理、识别或笔记生成模块。"""
from .metadata import parse_video
from .download import download_video

__all__ = ["parse_video", "download_video"]
