from sqlalchemy import select
from ..db import Task, ACTIVE_TASK_STATUSES


# 集中定义单个视频的活动任务查询，创建和恢复共用相同状态范围。
def active_task_query(video_id):
    """同一视频的活动分析任务；调用者按需保留任务或只投影编号。"""
    return select(Task).where(Task.video_id == video_id, Task.status.in_(ACTIVE_TASK_STATUSES))
