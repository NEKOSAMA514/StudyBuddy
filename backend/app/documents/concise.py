"""基于固定来源范围生成简洁文档笔记，并保留可恢复的分段记录。"""
import json
import time
from ..analysis.checkpoints import checkpoint_path
from ..util.json_io import atomic_json
from ..analysis.activity import emit
from ..analysis.concise_material import plan_segments, trailing_context, validate_coverage, validate_contexts
from ..analysis.content_types import complete_content_type
from ..analysis.markdown import validate_markdown
from ..analysis.schemas import ConciseNote
from ..errors import AppError
from .material import digest, verify_snapshot
from ..errors import DocumentStopped
from .versions import FORMAT_VERSION, CHECKPOINT_VERSION
from .formatting import render_sources


def sources_for(material):
    return [{"id": u["id"], "text": u["text"], "label": u["label"]} for u in material["units"] if u["text"].strip()]


# 沿文档来源位置和字符偏移规划简洁片段，保持材料覆盖可核对。
def plan(material, model, title):
    build = lambda primary, context: {"document_title": title, "primary": primary, "context": context}
    return plan_segments(sources_for(material), build, lambda p: model.input_characters("concise", p),
                         model.settings.llm_input_text_limit, lambda processed, _: trailing_context(processed, 500))


def validate_result(result, material):
    sources = sources_for(material)
    by_id = {s["id"]: s for s in sources}
    groups = []
    for number, chapter in enumerate(result["chapters"], 1):
        if chapter["chapter_id"] != f"S{number}":
            raise AppError("invalid_checkpoint", "简洁笔记片段编号不连续。")
        items = []
        for span in chapter["source_spans"]:
            source = by_id.get(span["unit_id"])
            if not source:
                raise AppError("invalid_checkpoint", "简洁笔记来源不存在。")
            items.append({**source, "start_offset": span["start_offset"], "end_offset": span["end_offset"],
                          "text": source["text"][span["start_offset"]:span["end_offset"]]})
        if chapter["source_ids"] != list(dict.fromkeys(i["id"] for i in items)):
            raise AppError("invalid_checkpoint", "简洁笔记来源与字符范围不匹配。")
        groups.append({"primary": items})
    validate_coverage(sources, groups, result["complete"])
    last = groups[-1]["primary"][-1]
    if (result["covered_until_unit"] != last["id"] or result["covered_until_offset"] != last["end_offset"] or
            result["source_cursor"] != {"unit_id": last["id"], "offset": last["end_offset"]}):
        raise AppError("invalid_checkpoint", "简洁笔记进度与来源字符不匹配。")


# 按片段生成简洁文档笔记，保存来源范围并支持复用已完成记录。
class ConciseDocumentGeneration:
    def __init__(self, task_id, folder, config, material, model, stop, check, stage, publish):
        self.task_id, self.folder, self.config, self.material, self.model = task_id, folder, config, material, model
        self.stop, self.checkpoint, self.stage, self.publish = stop, check, stage, publish
        self.timings = {}
        self.path = lambda op, key: checkpoint_path(folder, task_id, CHECKPOINT_VERSION, op,
                                                   digest([config["generation_key"], key])[:16])

    def check(self):
        if self.stop.is_set():
            raise DocumentStopped()
        self.checkpoint()

    async def run(self):
        self.check()
        verify_snapshot(self.material, self.config["source"])
        path = self.path("concise-manifest", "manifest")
        if path.exists():
            manifest = json.loads(path.read_text("utf-8"))
            if manifest["source"] != self.config["source"] or manifest["key"] != self.config["generation_key"]:
                raise AppError("invalid_checkpoint", "简洁分段清单与材料不一致。")
            segments = manifest["segments"]
        else:
            segments = plan(self.material, self.model, self.config["document_title"])
            atomic_json(path, {"source": self.config["source"], "key": self.config["generation_key"], "segments": segments})
        validate_coverage(sources_for(self.material), segments, True)
        validate_contexts(sources_for(self.material), segments)
        type_path = self.path("concise-type", "type")
        content_type = complete_content_type(json.loads(type_path.read_text("utf-8"))) if type_path.exists() else None
        chapters, tick = [], time.monotonic()
        try:
            for number, segment in enumerate(segments, 1):
                self.check()
                self.stage("concise", number, len(segments))
                payload = {"document_title": self.config["document_title"], **segment}
                if content_type:
                    payload["content_type"] = content_type
                labels = {item["id"]: item["label"] for item in segment["primary"]}
                def validate(raw):
                    value = ConciseNote.model_validate(raw).model_dump()
                    if content_type and value["content_type"] != content_type:
                        raise ValueError("content_type_must_match_planned_document_type")
                    validate_markdown(value["markdown"])
                    render_sources(value["markdown"], labels)
                    return value
                cache = self.path("concise-body", [number, segment, content_type if number > 1 else None])
                if cache.exists():
                    value = validate(json.loads(cache.read_text("utf-8")))
                    emit(self.model, "cached", operation="concise")
                else:
                    value = validate(await self.model.generate("concise", payload, validate))
                emit(self.model, "saving", operation="concise")
                atomic_json(cache, value)
                content_type = value["content_type"]
                atomic_json(type_path, content_type)
                items = segment["primary"]
                chapters.append({"chapter_id": f"S{number}", "title": value["title"], "markdown": render_sources(value["markdown"], labels),
                    "source_ids": list(dict.fromkeys(i["id"] for i in items)),
                    "source_spans": [{"unit_id": i["id"], "start_offset": i["start_offset"], "end_offset": i["end_offset"]} for i in items],
                    "source_labels": list(dict.fromkeys(i["label"] for i in items))})
                result = {"format_version": FORMAT_VERSION, "kind": "document", "note_style": "concise", "content_type": content_type,
                    "title": value["title"] if len(segments) == 1 else self.config["document_title"],
                    "tags": [], "overview_paragraphs": [], "source_issues": [], "segment_guides": [], "chapters": list(chapters),
                    "complete": number == len(segments), "completed_segments": number, "total_segments": len(segments),
                    "covered_until_unit": items[-1]["id"], "covered_until_offset": items[-1]["end_offset"],
                    "source_cursor": {"unit_id": items[-1]["id"], "offset": items[-1]["end_offset"]},
                    "source": self.config["source"], "model": self.config["model"], "prompt_version": self.config["prompt_version"]}
                validate_result(result, self.material)
                self.publish({**result, "complete": False})
                self.check()
            return result
        finally:
            self.timings["concise"] = round(time.monotonic() - tick, 3)
