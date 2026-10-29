"""按文档位置生成笔记；不依赖视频证据、时间、OCR或视频生成器。"""
import asyncio
from ..analysis.callbacks import ChapterCallbacks
import json
import time
from ..errors import AppError, DocumentStopped
from ..analysis.budgets import MAX_BATCH_CHAPTERS, OUTPUT_TOKENS, OUTPUT_BUDGET_RATIO
from ..analysis.checkpoints import checkpoint_path
from ..util.json_io import atomic_json
from ..analysis.activity import emit
from ..analysis.markdown import validate_markdown
from ..analysis.schemas import SourceIssue
from .material import digest, preceding, detailed_segments, compact_outline
from .schemas import DOCUMENT_SCHEMAS
from .versions import FORMAT_VERSION, CHECKPOINT_VERSION


class DetailedDocumentGeneration:
    def __init__(self, task_id, folder, config, material, model, stop, check, stage, publish):
        self.task_id, self.folder, self.config = task_id, folder, config
        self.model, self.stop, self.check, self.stage, self.publish = model, stop, check, stage, publish
        self.fragments, self.segments = detailed_segments(material, model, config["document_title"])
        self.sources = {f["id"]: f for f in self.fragments}
        self.indexes = {f["id"]: i for i, f in enumerate(self.fragments)}
        self.records, self.timings = [], {}
        self.content_type = None

    # 只精简请求副本中的前章大纲，并附加本段已确定的材料类型。
    def input_payload(self, payload):
        def compact(item):
            if "previous_chapter_outline" in item:
                return {**item, "previous_chapter_outline": compact_outline(item["previous_chapter_outline"])}
            return item
        payload = compact(payload)
        if "chapter" in payload:
            payload = {**payload, "chapter": compact(payload["chapter"])}
        if "chapters" in payload:
            payload = {**payload, "chapters": [compact(chapter) for chapter in payload["chapters"]]}
        return {**payload, "content_type": self.content_type} if self.content_type else payload

    def path(self, operation, payload):
        key = digest([self.config["generation_key"], operation, self.input_payload(payload)])[:16]
        return checkpoint_path(self.folder, self.task_id, CHECKPOINT_VERSION, operation, key)

    def fits(self, operation, payload):
        limit = self.config["llm_input_text_limit"]
        return self.model.input_characters(operation, self.input_payload(payload)) <= limit

    # 校验章节顺序、正文协议及真实引文，防止模型输出改变来源关系。
    def validate(self, operation, payload, value):
        value = DOCUMENT_SCHEMAS[operation].model_validate(value).model_dump()
        if operation == "outline":
            if payload.get("content_type") and value["content_type"] != payload["content_type"]:
                raise ValueError("content_type_must_match_planned_document_type")
            anchors = payload["anchors"]
            ids = [p["start_anchor_id"] for p in value["chapters"]]
            if any(i not in anchors for i in ids) or ids[0] != anchors[0] or any(
                    anchors.index(a) >= anchors.index(b) for a, b in zip(ids, ids[1:])):
                raise ValueError("document_chapter_boundaries_must_increase_from_first_anchor")
        elif operation == "chapters":
            if [c["chapter_id"] for c in value["chapters"]] != [c["chapter_id"] for c in payload["chapters"]]:
                raise ValueError("chapter_ids_must_match_request_order")
            for chapter in value["chapters"]:
                validate_markdown(chapter["markdown"])
        elif operation == "issue_quotes":
            requests = payload["repairs"]
            if [(r["chapter_id"], r["block_index"]) for r in value["repairs"]] != [
                    (r["chapter_id"], r["block_index"]) for r in requests]:
                raise ValueError("quote_repairs_must_match_request")
            sources = {s["id"]: s["text"] for s in payload["sources"]}
            if any(not repair["quote"] or not any(repair["quote"] in sources[i] for i in request["evidence_ids"])
                   for repair, request in zip(value["repairs"], requests)):
                raise ValueError("quote_must_be_continuous_original_text")
        return value

    # 校验后复用精确检查点，未命中时先核对预算再请求并原子保存。
    async def call(self, operation, payload, validator=None):
        self.check()
        payload = self.input_payload(payload)
        validate = validator or (lambda value: self.validate(operation, payload, value))
        path = self.path(operation, payload)
        if path.exists():
            value = validate(json.loads(path.read_text(encoding="utf-8")))
            emit(self.model, "cached", operation=operation)
            return value
        if not self.fits(operation, payload):
            raise AppError("model_input_budget", "文档请求超过输入预算，成功检查点已保留。")
        value = validate(await self.model.generate(operation, payload, validate))
        emit(self.model, "saving", operation=operation)
        atomic_json(path, value)
        emit(self.model, "saved", operation=operation)
        self.check()
        return value

    def context(self, fragment):
        return [{"id": c["id"], "text": c["text"]} for c in preceding(self.fragments, self.indexes[fragment["id"]])]

    # 根据实际请求大小装包，单条过长时二分切片，保留全部文字。
    def pack(self, items, build, operation, field="text"):
        groups, batch = [], []
        if not self.fits(operation, build([])):
            raise AppError("model_input_budget", "文档大纲或上下文过长，没有留下材料空间。")
        for item in items:
            remaining = dict(item)
            while not self.fits(operation, build([remaining])):
                if batch:
                    groups.append(batch)
                    batch = []
                text = remaining.get(field, "")
                low, high = 0, len(text)
                while low < high:
                    mid = (low + high + 1) // 2
                    if self.fits(operation, build([{**remaining, field: text[:mid]}])):
                        low = mid
                    else:
                        high = mid - 1
                if not low:
                    raise AppError("model_input_budget", "单条文档材料无法放入预算，检查点已保留。")
                groups.append([{**remaining, field: text[:low]}])
                remaining[field] = text[low:]
            if batch and not self.fits(operation, build([*batch, remaining])):
                groups.append(batch)
                batch = []
            batch.append(remaining)
        if batch:
            groups.append(batch)
        return groups

    # 超预算章节先提取事实并检查压缩是否收敛，避免无限重复请求。
    async def reduce(self, chapter, summary):
        reduced, previous = dict(chapter), None
        while not self.fits("chapters", {"document_summary": summary, "chapters": [reduced]}):
            field = "facts" if "facts" in reduced else "primary"
            make = lambda items: {"document_summary": summary, "chapter": {
                **{k: v for k, v in reduced.items() if k not in {"primary", "facts"}}, field: items}}
            values = [await self.call("extract", make(group)) for group in self.pack(reduced[field], make, "extract")]
            facts = [f for value in values for f in value["facts"]]
            length = sum(len(f["text"]) for f in facts)
            if not facts or previous is not None and length >= previous:
                raise AppError("model_input_budget", "文档材料压缩后仍超预算且未继续缩小，请拆分文档。")
            previous = length
            reduced = {**{k: v for k, v in reduced.items() if k != "primary"}, "facts": facts,
                       "issues": [*reduced.get("issues", []), *[i for v in values for i in v["issues"]]]}
        return reduced

    def body_path(self, chapter, summary):
        return self.path("chapter", {"document_summary": summary, "chapter": chapter})

    # 复用成功章节，逐章确认写盘；接口截断时只拆分尚未完成的批次。
    async def write_batch(self, group, summary):
        payload = {"document_summary": summary, "chapters": group}
        drafts = {}
        for chapter in group:
            path = self.body_path(chapter, summary)
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                self.validate("chapters", {"chapters": [chapter]}, {"chapters": [value]})
                drafts[chapter["chapter_id"]] = value
                emit(self.model, "cached", operation="chapters")
        missing = [c for c in group if c["chapter_id"] not in drafts]
        if not missing:
            return [drafts[c["chapter_id"]] for c in group]
        current = {**payload, "chapters": missing}
        def validate(value):
            return self.validate("chapters", current, value)
        def one(draft):
            request = next(c for c in missing if c["chapter_id"] == draft["chapter_id"])
            return self.validate("chapters", {"chapters": [request]}, {"chapters": [draft]})["chapters"][0]
        async def accept(draft):
            request = next(c for c in missing if c["chapter_id"] == draft["chapter_id"])
            atomic_json(self.body_path(request, summary), draft)
            emit(self.model, "saved", operation="chapters")
        validate = ChapterCallbacks(validate, one, accept)
        split = self.path("chapters", current).with_suffix(".split.json")
        try:
            if split.exists() and len(missing) > 1:
                raise AppError("model_output_truncated", "复用已确认的拆批标记。")
            value = await self.call("chapters", current, validate)
            for draft in value["chapters"]:
                await accept(draft)
                drafts[draft["chapter_id"]] = draft
        except AppError as exc:
            if exc.code != "model_output_truncated" or len(missing) < 2:
                raise
            atomic_json(split, {"reason": exc.code})
            middle = len(missing) // 2
            for part in (missing[:middle], missing[middle:]):
                for draft in await self.write_batch(part, summary):
                    drafts[draft["chapter_id"]] = draft
        return [drafts[c["chapter_id"]] for c in group]

    # 同时按输入、输出和并发预算调度章节批次，失败时取消其余请求。
    async def writing(self, chapters, summary):
        prepared = [await self.reduce(c, summary) for c in chapters]
        groups, group = [], []
        for chapter in prepared:
            trial = [*group, chapter]
            output = len(json.dumps(trial, ensure_ascii=False).encode("utf-8")) + 512 * len(trial)
            if group and (len(group) >= MAX_BATCH_CHAPTERS or
                          not self.fits("chapters", {"document_summary": summary, "chapters": trial}) or
                          output > self.config.get("llm_output_tokens", OUTPUT_TOKENS)["chapters"] * OUTPUT_BUDGET_RATIO):
                groups.append(group)
                group = []
            group.append(chapter)
        if group:
            groups.append(group)
        # 同一时间最多调度配置允许的批次数，避免一次创建全部请求。
        results, pending = {}, list(groups)
        while pending:
            active = [asyncio.create_task(self.write_batch(g, summary))
                      for g in pending[:self.model.settings.llm_section_concurrency]]
            pending = pending[len(active):]
            failure = None
            try:
                for future in asyncio.as_completed(active):
                    for draft in await future:
                        results[draft["chapter_id"]] = draft
                self.check()
            except BaseException as exc:
                failure = exc
            finally:
                for future in active:
                    if not future.done():
                        future.cancel()
                await asyncio.gather(*active, return_exceptions=True)
            if failure:
                raise failure
        return [results[c["chapter_id"]] for c in chapters]

    def spans(self, ids):
        return [{k: self.sources[i][k] for k in ("unit_id", "start_offset", "end_offset")} for i in ids]

    # 将存疑项绑定到真实材料位置，无效引文只修正一次，失败则保留原因。
    async def issues(self, chapter, draft):
        result = []
        primary = {p["id"] for p in chapter["primary"]}
        context = {c["id"] for c in chapter["context"]}
        for raw in draft["issues"]:
            try:
                issue = SourceIssue.model_validate(raw).model_dump()
            except ValueError:
                continue
            ids = list(dict.fromkeys(issue["evidence_ids"]))
            if not set(ids) <= primary | context or not primary.intersection(ids):
                if not issue["critical"]:
                    continue
                ids, issue["quote"] = list(p["id"] for p in chapter["primary"]), None
            if issue["quote"] and not any(issue["quote"] in self.sources[i]["text"] for i in ids):
                repair = {"chapter_id": chapter["chapter_id"], "block_index": 0, "quote": issue["quote"],
                          "reason": issue["reason"], "evidence_ids": ids}
                payload = {"repairs": [repair], "sources": [{"id": i, "text": self.sources[i]["text"]} for i in ids]}
                try:
                    issue["quote"] = (await self.call("issue_quotes", payload))["repairs"][0]["quote"]
                except AppError as exc:
                    if exc.code != "invalid_model_output":
                        raise
                    issue["quote"] = None
            result.append({**{k: v for k, v in issue.items() if k != "evidence_ids"}, "chapter_id": chapter["chapter_id"],
                           "source_ids": list(dict.fromkeys(self.sources[i]["unit_id"] for i in ids)), "source_spans": self.spans(ids)})
        return result

    # 只汇总各段已生成的摘要，必要时分层压缩，避免再次发送完整材料。
    async def overview(self, records):
        summaries = [{"title": r["outline"]["document_summary"], "summary": "\n".join(c["summary"] for c in r["chapters"])} for r in records]
        make = lambda items: {"document_summary": "", "summaries": items}
        while not self.fits("overview", make(summaries)):
            previous = len(json.dumps(summaries, ensure_ascii=False))
            reduced = []
            for group in self.pack(summaries, make, "overview", "summary"):
                value = await self.call("overview", make(group))
                reduced.append({"title": value["title"], "summary": "\n".join(value["overview_paragraphs"])})
            if len(json.dumps(reduced, ensure_ascii=False)) >= previous:
                raise AppError("model_input_budget", "文档摘要汇总未收敛，已保留章节笔记。")
            summaries = reduced
        return await self.call("overview", make(summaries))

    def result(self, complete=False, overview=None):
        chapters = [c for r in self.records for c in r["chapters"]]
        end = self.records[-1]["end"]
        return {"format_version": FORMAT_VERSION, "kind": "document", "note_style": "detailed", "complete": complete, "content_type": self.content_type,
                "title": self.config["document_title"], "tags": [], "overview_paragraphs": [], **(overview or {}),
                "chapters": chapters, "source_issues": [i for r in self.records for i in r["issues"]],
                "segment_guides": [r["outline"]["document_summary"] for r in self.records],
                "completed_segments": len(self.records), "total_segments": len(self.segments),
                "covered_until_unit": end["unit_id"], "covered_until_offset": end["end_offset"],
                "source": self.config["source"], "model": self.config["model"], "prompt_version": self.config["prompt_version"]}

    # 按文档来源顺序逐段规划和生成，每段完成即可发布可读结果。
    async def run(self):
        previous, count = {}, 0
        for number, segment in enumerate(self.segments, 1):
            self.check()
            self.stage("planning", number, len(self.segments))
            tick = time.monotonic()
            payload = {"document_title": self.config["document_title"], "anchors": [f["id"] for f in segment],
                       "evidence": [{"id": f["id"], "text": f["text"]} for f in segment],
                       "context": self.context(segment[0]), "previous_chapter_outline": previous}
            try:
                outline = await self.call("outline", payload)
                self.content_type = outline["content_type"]
            finally:
                self.timings["planning"] = self.timings.get("planning", 0) + time.monotonic() - tick
            self.stage("summarizing", number, len(self.segments), outline["document_summary"])
            chapters = []
            starts = [payload["anchors"].index(p["start_anchor_id"]) for p in outline["chapters"]] + [len(segment)]
            for index, plan in enumerate(outline["chapters"]):
                original = segment[starts[index]:starts[index + 1]]
                chapters.append({**plan, "chapter_id": f"C{count + index + 1}",
                                 "primary": [{"id": f["id"], "text": f["text"]} for f in original],
                                 "context": self.context(original[0]), "previous_chapter_outline": previous})
                previous = {k: plan[k] for k in ("title", "core_question", "topics")}
            tick = time.monotonic()
            try:
                drafts = await self.writing(chapters, outline["document_summary"])
                body, issues = [], []
                for chapter, draft in zip(chapters, drafts):
                    ids = [p["id"] for p in chapter["primary"]]
                    body.append({"chapter_id": chapter["chapter_id"], "title": chapter["title"], "summary": draft["summary"],
                                 "markdown": draft["markdown"], "source_ids": list(dict.fromkeys(self.sources[i]["unit_id"] for i in ids)),
                                 "source_spans": self.spans(ids), "source_labels": list(dict.fromkeys(self.sources[i]["label"] for i in ids))})
                    issues.extend(await self.issues(chapter, draft))
            finally:
                self.timings["summarizing"] = self.timings.get("summarizing", 0) + time.monotonic() - tick
            count += len(chapters)
            self.records.append({"outline": outline, "chapters": body, "issues": issues, "end": segment[-1]})
            self.publish(self.result())
        self.stage("finalizing", len(self.segments), len(self.segments))
        tick = time.monotonic()
        try:
            overview = await self.overview(self.records)
        finally:
            self.timings["finalizing"] = time.monotonic() - tick
        self.check()
        return self.result(True, overview)
