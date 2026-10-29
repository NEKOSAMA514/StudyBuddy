"""直接生成简洁视频笔记，按来源字符而非相邻时间检查覆盖。"""
import json
import time
from ..analysis.checkpoints import checkpoint_path
from ..util.json_io import atomic_json
from ..analysis.activity import emit
from ..analysis.concise_material import plan_segments, validate_coverage, validate_contexts
from ..analysis.markdown import validate_markdown
from ..analysis.schemas import ConciseNote
from ..errors import AppError, TaskPaused
from .schemas import FORMAT_VERSION
from .versions import PROMPT_VERSION, digest
from .timestamps import render_timestamps


def payload_for(title, primary, context):
    # 别名仅用于本次请求，完整来源和字符范围仍保存在材料清单中。
    aliases = {identifier: f"E{index + 1}" for index, identifier in
               enumerate(dict.fromkeys(item["id"] for item in [*primary, *context]))}
    def compact(items):
        return [{"id": aliases[item["id"]], "kind": item["kind"], "text": item["text"]} for item in items]
    return {"video_title": title, "primary": compact(primary), "context": compact(context)}


def context_before(processed, source):
    left = max(0, source["start_seconds"] - 120)
    return [item for item in processed if item["id"] == source["id"] or
            item["start_seconds"] >= left or item["end_seconds"] > left]


# 按来源字符与实际请求预算规划简洁片段，前文不计入主要覆盖范围。
def plan(sources, model, limit, video_title):
    return plan_segments(sources, lambda primary, context: payload_for(video_title, primary, context),
                         lambda p: model.input_characters("concise", p), limit, context_before)


def validate_result(result, evidence, duration):
    sources = sorted(evidence.values(), key=lambda s: (s["start_seconds"], s["end_seconds"], s["id"]))
    if source_signature(sources) != result["source_digest"]:
        raise AppError("invalid_evidence", "简洁笔记的原始材料已改变，请重新生成。")
    groups = []
    for number, chapter in enumerate(result["chapters"], 1):
        if chapter["chapter_id"] != f"S{number}":
            raise AppError("invalid_checkpoint", "简洁笔记片段编号不连续。")
        items = []
        for span in chapter["evidence_spans"]:
            source = evidence.get(span["evidence_id"])
            if source is None:
                raise AppError("invalid_evidence", "简洁笔记来源不存在。")
            items.append({**source, "start_offset": span["start_offset"], "end_offset": span["end_offset"],
                          "text": source["text"][span["start_offset"]:span["end_offset"]]})
        if chapter["evidence_ids"] != list(dict.fromkeys(i["id"] for i in items)):
            raise AppError("invalid_evidence", "简洁笔记依据与字符范围不匹配。")
        if (chapter["start_seconds"] != min(i["start_seconds"] for i in items) or
                chapter["end_seconds"] != max(i["end_seconds"] for i in items)):
            raise AppError("invalid_timestamp", "简洁笔记定位与原记录不匹配。")
        groups.append({"primary": items})
    index, offset = validate_coverage(sources, groups, result["complete"])
    expected = duration if index == len(sources) else sources[index]["start_seconds"]
    last = groups[-1]["primary"][-1]
    if (result["covered_until_seconds"] != expected or result["source_cursor"] != {
            "evidence_id": last["id"], "offset": last["end_offset"]}):
        raise AppError("invalid_checkpoint", "简洁笔记覆盖进度与字符范围不一致。")
    return result["chapters"]


def source_signature(sources):
    return digest([{k: s[k] for k in ("id", "kind", "text", "start_seconds", "end_seconds")} for s in sources])


# 逐段生成并保存简洁视频笔记，恢复时复用输入身份一致的完成记录。
class ConciseVideoGeneration:
    def __init__(self, task_id, model, settings, folder, evidence, duration, stop, check, update, timings,
                 publish, video_title):
        self.model, self.stop, self.checkpoint, self.update = model, stop, check, update
        self.sources = sorted([s for s in evidence if s["text"].strip()], key=lambda s: (s["start_seconds"], s["end_seconds"], s["id"]))
        self.duration, self.timings, self.publish, self.title = duration, timings, publish, video_title
        self.build = lambda primary, context: payload_for(video_title, primary, context)
        self.key = digest({"source": self.sources, "title": video_title, "style": "concise", "prompt": PROMPT_VERSION,
            "input_format": "id-kind-text-v1",
            "model": settings.llm_model, "base": settings.llm_base_url, "profile": settings.llm_profile_id,
            "provider": settings.llm_provider, "stream": settings.llm_stream, "json": settings.llm_json_mode,
            "summary_prompt": settings.summary_prompt, "input_limit": settings.llm_input_text_limit,
            "outputs": settings.llm_output_tokens})
        self.path = lambda operation, key: checkpoint_path(folder, task_id, FORMAT_VERSION, operation, digest([self.key, key]))
        self.limit = settings.llm_input_text_limit

    def check(self):
        if self.stop.is_set():
            raise TaskPaused()
        self.checkpoint()

    async def run(self):
        self.check()
        path = self.path("concise-manifest", "manifest")
        if path.exists():
            manifest = json.loads(path.read_text("utf-8"))
            if manifest["key"] != self.key:
                raise AppError("invalid_checkpoint", "简洁分段清单与任务不一致。")
            segments = manifest["segments"]
        else:
            segments = plan(self.sources, self.model, self.limit, self.title)
            atomic_json(path, {"key": self.key, "segments": segments})
        validate_coverage(self.sources, segments, True)
        validate_contexts(self.sources, segments)
        type_path, content_type = self.path("concise-type", "type"), None
        if type_path.exists():
            from ..analysis.content_types import complete_content_type
            content_type = complete_content_type(json.loads(type_path.read_text("utf-8")))
        chapters = []
        tick = time.monotonic()
        try:
            for number, segment in enumerate(segments, 1):
                self.check()
                self.update(f"concise:{number}/{len(segments)}")
                payload = self.build(segment["primary"], segment["context"])
                time_sources = {item["id"]: source["start_seconds"]
                                for item, source in zip(payload["primary"], segment["primary"])}
                if content_type:
                    payload["content_type"] = content_type
                def validate(raw):
                    value = ConciseNote.model_validate(raw).model_dump()
                    if content_type and value["content_type"] != content_type:
                        raise ValueError("content_type_must_match_planned_video_type")
                    validate_markdown(value["markdown"])
                    render_timestamps(value["markdown"], time_sources)
                    return value
                # 第一条记录始终使用不含类型的键，恢复材料类型后也保持一致。
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
                chapters.append({"chapter_id": f"S{number}", "title": value["title"],
                    "markdown": render_timestamps(value["markdown"], time_sources),
                    "evidence_ids": list(dict.fromkeys(i["id"] for i in items)),
                    "evidence_spans": [{"evidence_id": i["id"], "start_offset": i["start_offset"], "end_offset": i["end_offset"]} for i in items],
                    "start_seconds": min(i["start_seconds"] for i in items), "end_seconds": max(i["end_seconds"] for i in items),
                    "timestamp_basis": "speech" if any(i["kind"] == "speech" for i in items) else "frame"})
                index, _ = validate_coverage(self.sources, segments[:number])
                result = {"format_version": FORMAT_VERSION, "note_style": "concise", "content_type": content_type,
                    "title": value["title"] if len(segments) == 1 else self.title, "tags": [], "overview_paragraphs": [],
                    "source_issues": [], "segment_guides": [], "chapters": list(chapters), "complete": number == len(segments),
                    "completed_segments": number, "total_segments": len(segments), "generation_key": self.key,
                    "source_cursor": {"evidence_id": items[-1]["id"], "offset": items[-1]["end_offset"]},
                    "covered_until_seconds": self.duration if index == len(self.sources) else self.sources[index]["start_seconds"],
                    "evidence_ids": list(dict.fromkeys(i for c in chapters for i in c["evidence_ids"])),
                    "source_ids": [i["id"] for i in self.sources], "source_digest": source_signature(self.sources)}
                validate_result(result, {s["id"]: s for s in self.sources}, self.duration)
                self.publish({**result, "complete": False})
                self.check()
            return result
        finally:
            self.timings["concise"] = round(time.monotonic() - tick, 3)
