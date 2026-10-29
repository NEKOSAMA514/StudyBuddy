from sqlalchemy import case, select
from ..db import DocumentTask, ACTIVE_TASK_STATUSES
from ..errors import AppError
from .schemas import public_task


# 列表优先返回活动任务，其后按更新时间和编号选择，避免恢复旧任务后选错。
def latest_task(session, identifier):
    task = session.scalar(select(DocumentTask).where(DocumentTask.document_id == identifier)
                          .order_by(case((DocumentTask.status.in_(ACTIVE_TASK_STATUSES), 0), else_=1),
                                    DocumentTask.updated_at.desc(), DocumentTask.id.desc()).limit(1))
    return public_task(task) if task else None


def get_task(session, identifier):
    task = session.get(DocumentTask, identifier)
    if task is None:
        raise AppError("document_task_missing", "文档任务不存在或已被删除。", 404)
    return task
