"""普通 JSON 文件的临时写入与原子替换；目录、锁和状态由调用方管理。"""
import json
from time import sleep


# 先写同目录临时 JSON 再替换目标，业务锁与状态发布顺序由调用方维护。
def atomic_json(path, value, *, ensure_ascii=False):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=ensure_ascii), encoding="utf-8")
    replace_file(temporary, path)


def replace_file(temporary, path):
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            # Windows 的读句柄或短暂文件占用可能阻止替换；其他错误及持续占用仍向上报告。
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                raise
            sleep(0.05 * 2 ** attempt)
