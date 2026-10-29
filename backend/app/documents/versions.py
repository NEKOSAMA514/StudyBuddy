"""生成流程版本与已保存笔记格式的版本独立维护。"""
from ..analysis.budgets import OUTPUT_TOKENS
from ..errors import AppError
from .material import digest
from .prompts import SYSTEM_DOCUMENT_PROMPT, DOCUMENT_INSTRUCTIONS, CONCISE_DOCUMENT_RULES
from .schemas import DOCUMENT_SCHEMAS

FORMAT_VERSION = 1
CHECKPOINT_VERSION = 1
FLOW_VERSION = "document-flow-v4-content-budget"

DOCUMENT_PROMPT_VERSION = "document-v1-" + digest([
    SYSTEM_DOCUMENT_PROMPT, DOCUMENT_INSTRUCTIONS, CONCISE_DOCUMENT_RULES, OUTPUT_TOKENS,
    {key: value.model_json_schema() for key, value in DOCUMENT_SCHEMAS.items()}])[:16]


def check_generation(config):
    if config.get("generation_flow") != FLOW_VERSION or config.get("prompt_version") != DOCUMENT_PROMPT_VERSION:
        raise AppError("document_generation_changed", "文档生成规则已更新，请重新生成笔记。", 409)


# 将影响文档生成的配置组成稳定身份，用于活动任务复用和恢复校验。
def generation_key(config):
    return digest({key: item for key, item in config.items() if key not in {"generation_key", "task_activity"}})
