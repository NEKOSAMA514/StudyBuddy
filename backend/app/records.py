from .db import Video, Task
from .errors import AppError


# 统一查找视频记录，不存在时返回稳定的业务错误。
def get_video(session, video_id):
    row = session.get(Video, video_id)
    if row is None:
        raise AppError("not_found", "视频不存在。", 404)
    return row

def get_task(session, task_id):
    row = session.get(Task, task_id)
    if row is None:
        raise AppError("not_found", "任务不存在。", 404)
    return row
