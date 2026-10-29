from app.analysis.content_types import CONTENT_TYPES
import asyncio
import json
from app.videos.model import ModelClient


def run_model(model, method, *args, **kwargs):
    async def call():
        async with model:
            return await getattr(model, method)(*args, **kwargs)
    return asyncio.run(call())


async def no_delay(seconds):
    pass


def outline_payload(items):
    anchors = [item for item in items if item["kind"] == "speech"] or items
    return {"anchors": [{"id": item["id"], "start_seconds": item["start_seconds"]} for item in anchors],
            "evidence": items}


def outline_result(identifier):
    return {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "主题", "core_question": "问题", "topics": ["内容"],
                           "start_anchor_id": identifier}], "video_summary": identifier}


def model_content(value):
    if not isinstance(value, dict) or not value.get("chapters") or any("chapter_id" not in c for c in value["chapters"]):
        return json.dumps(value, ensure_ascii=False)
    lines = []
    for chapter in value["chapters"]:
        lines.extend([f'<!-- chapter:{chapter["chapter_id"]} -->', f'<!-- summary:{chapter.get("summary", "")} -->'])
        lines.append(chapter.get("markdown", ""))
        for issue in chapter.get("issues", []):
            lines.append('<!-- issue:' + json.dumps({k: v for k, v in issue.items() if k != "kind"}, ensure_ascii=False) + ' -->')
    return "\n\n".join(lines)


class FakeRecognizer:
    def __init__(self, settings):
        self.settings = settings
        self.asr_calls = 0
        self.ocr_calls = 0

    def transcribe(self, path):
        self.asr_calls += 1
        return [{"start_seconds": 0, "end_seconds": 1.5, "text": "慢启动让拥塞窗口逐步增长。"}]

    def ocr(self, path):
        self.ocr_calls += 1
        return "TCP 慢启动：拥塞窗口增长", 95


class FakeModel(ModelClient):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def generate(self, operation, payload, validate):
        self.calls += 1
        self.usage_reports += 1
        if operation == "concise":
            value = {"content_type": payload.get("content_type", CONTENT_TYPES["课程或教材"]),
                     "title": "简洁笔记重点", "markdown": "**核心结论**：材料解释了主要概念与适用条件。"}
            if "video_title" in payload:
                value["markdown"] = f"## 核心概念 [回看](#source-{payload['primary'][0]['id']})\n\n" + value["markdown"]
            else:
                value["markdown"] = f"## 核心概念 [来源](#source-{payload['primary'][0]['id']})\n\n" + value["markdown"]
        elif operation == "outline":
            value = {"content_type": payload.get("content_type", CONTENT_TYPES["课程或教材"]), "chapters": [{"title": "慢启动", "core_question": "拥塞窗口如何增长？",
                     "topics": ["拥塞窗口增长"], "start_anchor_id": (payload["anchors"] or [{"id": "unused"}])[0]["id"]}],
                     "video_summary": "视频介绍 TCP 慢启动与拥塞窗口增长。"}
        elif operation == "extract":
            chapter = payload["chapter"]
            value = {"facts": chapter.get("facts") or [{"text": item["text"]}
                     for item in chapter["primary"]], "issues": chapter.get("issues", [])}
        elif operation == "chapters":
            drafts = []
            for chapter in payload["chapters"]:
                drafts.append({"chapter_id": chapter["chapter_id"], "summary": "介绍慢启动中的拥塞窗口增长。",
                               "markdown": "窗口在慢启动阶段增长。", "issues": chapter.get("issues", [])})
            value = {"chapters": drafts}
        else:
            value = {"title": "TCP 慢启动与拥塞窗口增长", "tags": ["TCP", "慢启动", "拥塞窗口"],
                     "overview_paragraphs": ["这是自动化测试返回的总结：视频介绍 TCP 慢启动。", "拥塞窗口逐步增长。"]}
        validate(value)
        return value


def model_validator(operation, validate):
    from app.analysis.callbacks import ChapterCallbacks
    from app.videos.schemas import SCHEMAS
    if operation != "chapters" or isinstance(validate, ChapterCallbacks):
        return validate
    async def accept(draft):
        pass
    def chapter(draft):
        return SCHEMAS["chapters"].model_validate({"chapters": [draft]}).model_dump()["chapters"][0]
    return ChapterCallbacks(validate, chapter, accept)
