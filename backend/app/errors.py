import json


class TaskInterrupted(Exception):
    """服务关闭；任务在安全点退出，保留已完成材料。"""


def model_error_message(raw):
    """接口消息优先；解析失败仍保留原始报错正文。"""
    if not raw.strip():
        return "出现了未知错误，请查看错误报告。"
    try:
        body = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return raw
    if isinstance(body, dict):
        error = body.get("error")
        for value in (error.get("message") if isinstance(error, dict) else None,
                      body.get("message"), error):
            if isinstance(value, str) and value.strip():
                return value
    return raw


# 携带稳定业务错误码、用户提示和 HTTP 状态，由统一请求边界转换为响应。
class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# 表示视频任务主动停止，和服务退出造成的中断分别处理。
class TaskPaused(Exception):
    """主动停止；成功检查点保留。"""


# 表示文档任务已不再运行，使生成流程停止并保留已经完成的草稿。
class DocumentStopped(Exception):
    """文档主动停止；成功章节保留。"""
