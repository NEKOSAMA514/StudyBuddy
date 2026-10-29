import json
from ..analysis.llm import ModelClient
from ..analysis.concise_prompt import concise_messages
from .prompts import SYSTEM_DOCUMENT_PROMPT, DOCUMENT_INSTRUCTIONS, CONCISE_DOCUMENT_RULES
from .schemas import DOCUMENT_SCHEMAS


# 只定义文档请求消息与输出契约，发送、修正和取消复用共用模型客户端。
class DocumentModelClient(ModelClient):
    schemas = DOCUMENT_SCHEMAS

    def messages(self, operation, payload):
        if operation == "concise":
            payload = {**payload, **{key: [{"id": item["id"], "text": item["text"]} for item in payload[key]]
                                      for key in ("primary", "context") if key in payload}}
            return concise_messages(payload, self.settings.summary_prompt, CONCISE_DOCUMENT_RULES)
        rule = ("直接输出带约定标记的Markdown，不输出JSON或包裹全文的代码围栏。" if operation == "chapters" else
                "只输出符合Schema的JSON对象：" + json.dumps(self.schemas[operation].model_json_schema(), ensure_ascii=False))
        messages = [{"role": "system", "content": SYSTEM_DOCUMENT_PROMPT + "\n" + DOCUMENT_INSTRUCTIONS[operation] + "\n" + rule}]
        if self.settings.summary_prompt:
            messages.append({"role": "user", "content": "用户笔记补充要求：\n" + self.settings.summary_prompt})
        messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})
        return messages
