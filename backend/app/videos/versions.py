import hashlib
import json
from .prompts import SYSTEM_SUMMARY_PROMPT, VIDEO_INSTRUCTIONS
from .schemas import SCHEMAS
from ..analysis.budgets import OUTPUT_TOKENS

# 版本由实际提示词、预算和输出结构计算，纯说明注释不改变任务兼容身份。
PROMPT_VERSION = "video-notes-v14-styles-characters-" + hashlib.sha256(
    json.dumps([SYSTEM_SUMMARY_PROMPT, VIDEO_INSTRUCTIONS, OUTPUT_TOKENS, {key: schema.model_json_schema() for key, schema in SCHEMAS.items()}],
               sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:12]


# 拒绝不兼容的生成流程或提示词快照，避免用新版逻辑续写旧任务。
def check_generation(config):
    from .segments import FLOW_VERSION
    from ..errors import AppError
    if config.get("generation_flow") != FLOW_VERSION or config.get("prompt_version") != PROMPT_VERSION:
        raise AppError("generation_changed", "笔记生成规则已更新，请重新生成笔记。", 409)


# 视频身份保留原 JSON 序列化和前 16 位摘要，与文档完整哈希分别维护。
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
