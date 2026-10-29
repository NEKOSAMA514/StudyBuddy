"""章节输出的显式校验及持久化确认接口。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterCallbacks:
    validate_batch: Callable[[dict], dict | None]
    validate_chapter: Callable[[dict], dict]
    # 模型客户端必须等待持久化确认，才能修正缺失章节或返回整批结果。
    accept: Callable[[dict], Awaitable[None]]

    def __call__(self, value: dict) -> dict | None:
        return self.validate_batch(value)
