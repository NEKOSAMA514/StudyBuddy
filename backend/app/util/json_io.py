"""普通 JSON 文件的临时写入与原子替换；目录、锁和状态由调用方管理。"""
import json


# 先写同目录临时 JSON 再替换目标，业务锁与状态发布顺序由调用方维护。
def atomic_json(path, value, *, ensure_ascii=False):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=ensure_ascii), encoding="utf-8")
    temporary.replace(path)
