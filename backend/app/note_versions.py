"""校验操作所针对的笔记仍是资料保留的完整版本。"""
from sqlalchemy import select

from .db import DocumentTask, Task
from .errors import AppError
from .videos.schemas import current_notes


def retained_note(session, kind, identifier, unavailable_message):
    model = Task if kind == "video" else DocumentTask
    task = session.get(model, identifier)
    if task is None:
        raise AppError("not_found", "笔记任务不存在。", 404)
    file_key = model.video_id if kind == "video" else model.document_id
    file_id = task.video_id if kind == "video" else task.document_id
    date = model.updated_at if kind == "video" else model.completed_at
    candidates = session.execute(select(model.id, model.result).where(file_key == file_id, model.status == "succeeded")
                                 .order_by(date.desc(), model.id.desc()))
    valid = 0
    for task_id, result in candidates:
        complete = current_notes(result) and result.get("complete", True) if kind == "video" else (
            result and result.get("complete") and result.get("chapters"))
        if complete:
            if task_id == identifier:
                return task
            valid += 1
            if valid == 2:
                break
    raise AppError("notes_unavailable", unavailable_message, 409)
