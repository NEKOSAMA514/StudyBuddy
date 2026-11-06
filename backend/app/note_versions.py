"""共用完整笔记的排序、保留和访问规则。"""
from itertools import islice

from sqlalchemy import select

from .db import DocumentTask, Task
from .errors import AppError
from .videos.schemas import current_notes


NOTE_VERSION_LIMIT = 2


def is_complete_note(kind, result):
    if kind == "video":
        return current_notes(result) and result.get("complete", True)
    return bool(result and result.get("complete") and result.get("chapters"))


def _version_fields(kind):
    if kind == "video":
        return Task, Task.video_id, Task.updated_at
    return DocumentTask, DocumentTask.document_id, DocumentTask.completed_at


def _version_query(kind, file_id):
    model, file_key, date = _version_fields(kind)
    return (select(model).where(file_key == file_id, model.status == "succeeded")
            .order_by(date.desc(), model.id.desc()).execution_options(yield_per=16))


def _complete_slice(kind, rows, *, retired=False):
    start, stop = (NOTE_VERSION_LIMIT, None) if retired else (0, NOTE_VERSION_LIMIT)
    try:
        complete = (row for row in rows if is_complete_note(kind, row.result))
        return list(islice(complete, start, stop))
    finally:
        rows.close()


def note_versions(session, kind, file_id, *, retired=False):
    return _complete_slice(kind, session.scalars(_version_query(kind, file_id)), retired=retired)


def retained_note(session, kind, identifier, unavailable_message):
    model, file_key, _ = _version_fields(kind)
    task = session.get(model, identifier)
    if task is None:
        raise AppError("not_found", "笔记任务不存在。", 404)
    query = _version_query(kind, getattr(task, file_key.key)).with_only_columns(model.id, model.result)
    if any(row.id == identifier for row in _complete_slice(kind, session.execute(query))):
        return task
    raise AppError("notes_unavailable", unavailable_message, 409)
