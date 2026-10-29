"""集中调整生成预算。文字按 Python 字符数统计，输出上限单位为 Token。"""
import json

# 各阶段统一统计输入文字；不计材料 JSON 包装和来源定位元数据。
DEFAULT_INPUT_TEXT_LIMIT = 25000

OUTPUT_TOKENS = {"outline": 32768, "extract": 32768,
                 "chapters": 32768, "overview": 16384, "issue_quotes": 32768,
                 "connection_test": 1024}

# 小章节合批时预留的输出空间，不限制正文实际长度。
OUTPUT_BUDGET_RATIO = 0.8

# 每批最多合并的章节数，仍须满足输入与输出预算。
MAX_BATCH_CHAPTERS = 5


def task_budgets(settings):
    return {"llm_input_text_limit": settings.llm_input_text_limit,
            "llm_output_tokens": dict(settings.llm_output_tokens)}


# 恢复任务保存的输入输出预算，旧快照缺少输出配置时使用固定兼容默认值。
def restore_task_budgets(settings, config):
    from dataclasses import replace
    return replace(settings,
                   llm_input_text_limit=config["llm_input_text_limit"],
                   llm_output_tokens=dict(config.get("llm_output_tokens", OUTPUT_TOKENS)))


def _text_characters(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_characters(item) for item in value)
    if isinstance(value, dict):
        return sum(_text_characters(item) for key, item in value.items()
                   if key not in {"id", "anchors", "kind", "label", "source_labels", "source_label",
                                  "image_name", "timestamp_basis", "format", "locator"}
                   and not key.endswith(("_id", "_ids")))
    return 0


# 计量真正发送的系统与材料文字，排除 JSON 包装及来源编号等元数据。
def message_characters(messages):
    total = 0
    for message in messages:
        content = message["content"]
        if message["role"] == "system":
            total += len(content)
            continue
        candidate = content.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate.partition("\n")[2].rsplit("```", 1)[0].strip()
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            total += len(content)
        else:
            total += _text_characters(value)
    return total
