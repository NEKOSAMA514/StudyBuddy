"""有界异步生成；协调流程独占检查点写入和任务进度。"""
import asyncio
from ..analysis.callbacks import ChapterCallbacks
import json
import logging
import time
from collections import deque
from .versions import PROMPT_VERSION, digest
from ..analysis.checkpoints import checkpoint_path
from ..util.json_io import atomic_json
from ..analysis.activity import emit
from .schemas import SCHEMAS, FORMAT_VERSION, ground_chapters
from ..analysis.schemas import SourceIssue
from ..analysis.budgets import OUTPUT_TOKENS, OUTPUT_BUDGET_RATIO, MAX_BATCH_CHAPTERS
from ..analysis.markdown import validate_markdown
from .model_input import outline_text_length, deduplicate_ocr
from .segments import FLOW_VERSION, split_segments, preceding_context, outline_measure
from ..errors import AppError, TaskPaused

logger = logging.getLogger("studybuddy")


class DetailedVideoGeneration:
    def __init__(self, task_id, model, settings, folder, evidence, duration, stop, checkpoint, update, timings,
                 publish=lambda result: None, segment_update=lambda progress: None, video_title="",
                 outline_ready=lambda summary: None):
        self.model, self.settings, self.folder = model, settings, folder
        self.stop, self.checkpoint, self.update, self.timings = stop, checkpoint, update, timings
        self.duration = duration
        self.video_title = video_title
        self.content_type = None
        self.publish, self.segment_update = publish, segment_update
        self.outline_ready = outline_ready
        self.range_start, self.range_end, self.chapter_offset = 0.0, duration, 0
        self.boundary, self.previous_chapter_outline = [], {}
        self.phase_update = lambda phase: None
        self.outline_digest = None
        ordered = sorted(evidence, key=lambda e: (e["start_seconds"], e["end_seconds"], e["id"]))
        self.original = {f"E{i + 1}": item for i, item in enumerate(ordered)}
        self.evidence = [{"id": alias, **{key: item[key] for key in ("kind", "start_seconds", "end_seconds", "text")}}
                         for alias, item in self.original.items()]
        self.anchors = deduplicate_ocr({"evidence": self.evidence})["evidence"]
        self.common = {"task_id": task_id, "flow": FLOW_VERSION, "prompt": PROMPT_VERSION, "model": settings.llm_model,
                       "provider": settings.llm_provider, "base": settings.llm_base_url,
                       "stream": settings.llm_stream, "json_mode": settings.llm_json_mode,
                       "summary_prompt": settings.summary_prompt,
                       "input_limit": settings.llm_input_text_limit,
                       "source": digest(ordered)}
        if settings.llm_output_tokens != OUTPUT_TOKENS:
            self.common["output_tokens"] = dict(settings.llm_output_tokens)
        if video_title:
            self.common["video_title"] = video_title
        self.base_common, self.all_evidence = self.common, self.evidence

    def check(self):
        if self.stop.is_set():
            raise TaskPaused()
        self.checkpoint()

    def fits(self, operation, payload, reserve=0):
        payload = self.input_payload(operation, payload)
        return self.model.input_characters(operation, payload) + reserve <= self.settings.llm_input_text_limit

    # 用实际请求计量装包，过长材料切片后仍保留来源和时间范围。
    def batches(self, operation, items, payload, reserve=0):
        """只在证据/信息边界拆分；单条过长时分片，原始 ID 和全文仍保留。"""
        if not self.fits(operation, payload([]), reserve):
            raise AppError("model_input_budget", "章节大纲与上下文超过输入预算，请在 API 配置的高级设置中增大输入上限。")
        groups, batch = [], []
        for item in items:
            remaining = dict(item)
            while not self.fits(operation, payload([remaining]), reserve):
                field = "text" if "text" in remaining else "quote"
                text = remaining.get(field, "")
                low, high = 0, len(text)
                while low < high:
                    middle = (low + high + 1) // 2
                    if self.fits(operation, payload([{**remaining, field: text[:middle]}]), reserve):
                        low = middle
                    else:
                        high = middle - 1
                if not low:
                    raise AppError("model_input_budget", "章节大纲与上下文没有留出材料空间，请在 API 配置的高级设置中增大输入上限。")
                if operation == "overview":
                    for separator in ("\n\n", "\n", "。", "！", "？", ". "):
                        boundary = text.rfind(separator, low // 2, low)
                        if boundary >= 0:
                            low = boundary + len(separator)
                            break
                if batch:
                    groups.append(batch)
                    batch = []
                groups.append([{**remaining, field: text[:low]}])
                remaining[field] = text[low:]
            if batch and not self.fits(operation, payload([*batch, remaining]), reserve):
                groups.append(batch)
                batch = []
            batch.append(remaining)
        if batch:
            groups.append(batch)
        return groups

    def path(self, operation, payload):
        payload = self.input_payload(operation, payload)
        key = {**self.common, "input": payload}
        if operation in {"extract", "chapters", "chapter", "overview", "issue_quotes"}:
            key["outline"] = self.outline_digest
        return checkpoint_path(self.folder, self.common["task_id"], FORMAT_VERSION, operation, digest(key))

    def input_payload(self, operation, payload):
        if self.video_title and operation in {"outline", "chapters", "extract", "overview"}:
            payload = {**payload, "video_title": self.video_title}
        if self.content_type and isinstance(payload, dict):
            payload = {**payload, "content_type": self.content_type}
        return payload

    def validate(self, operation, payload, value):
        value = SCHEMAS[operation].model_validate(value).model_dump()
        if operation == "outline":
            if payload.get("content_type") and value["content_type"] != payload["content_type"]:
                raise ValueError("content_type_must_match_planned_video_type")
            times = {item["id"]: item["start_seconds"] for item in payload["anchors"]}
            ids = [item["start_anchor_id"] for item in value["chapters"]]
            invalid = [identifier for identifier in ids if identifier not in times]
            if invalid:
                raise ValueError("invalid_boundary_anchor: start_anchor_id 不在本段原文中：" +
                                 json.dumps(invalid, ensure_ascii=False) + "。请从本段 evidence 中选择，不要编造或改写编号。")
            if not ids or ids[0] != payload["anchors"][0]["id"] or any(
                    times[a] >= times[b] for a, b in zip(ids, ids[1:])):
                raise ValueError("chapter_boundaries_must_increase_from_first_anchor")
        elif operation == "chapters":
            expected = [item["chapter_id"] for item in payload["chapters"]]
            if [item["chapter_id"] for item in value["chapters"]] != expected:
                raise ValueError("chapter_ids_must_match_request_order")
            for chapter in value["chapters"]:
                validate_markdown(chapter["markdown"])
        elif operation == "issue_quotes":
            expected = payload["repairs"]
            repairs = value["repairs"]
            if [(i["chapter_id"], i["block_index"]) for i in repairs] != [
                    (i["chapter_id"], i["block_index"]) for i in expected]:
                raise ValueError("issue_quote_repairs_must_match_request_order")
            sources = {i["id"]: i["text"] for i in payload["sources"]}
            if any(not any(item["quote"] in sources[i] for i in request["evidence_ids"])
                   for item, request in zip(repairs, expected)):
                raise ValueError("issue_quote_must_be_original_text")
        return value

    def body_path(self, chapter):
        return getattr(self, "body_paths", {}).get(chapter["chapter_id"], self.path("chapter", chapter))

    async def pool(self, jobs, completed=lambda job, value: None):
        """回调可返回就绪作业；共用调度与缓存，返回初始作业的有序结果。"""
        initial = list(jobs)
        jobs, results, active = [], [], {}
        pending, ready = deque(), deque()
        self.failed_job = None
        keys, aliases = {}, {}
        failure = None
        partials = deque()
        partial_ready = asyncio.Event()

        def save_chapters(payload, value):
            for draft in value["chapters"]:
                request = next(c for c in payload["chapters"] if c["chapter_id"] == draft["chapter_id"])
                atomic_json(self.body_path(request), draft)

        # 协调循环统一写入单章检查点，完成后才解除模型客户端的保存等待。
        def collect_partials():
            partial_ready.clear()
            while partials:
                payload, draft, saved = partials.popleft()
                request = next(c for c in payload["chapters"] if c["chapter_id"] == draft["chapter_id"])
                single = {"chapters": [request]}
                value = self.validate("chapters", single, {"chapters": [draft]})
                save_chapters(single, value)
                emit(self.model, "saved", operation="chapters")
                register(completed(("chapters", single), value) or [], priority=True)
                if not saved.done():
                    saved.set_result(None)

        def register(new_jobs, priority=False):
            missing = []
            for operation, payload in new_jobs:
                self.check()
                payload = self.input_payload(operation, payload)
                index = len(jobs)
                jobs.append((operation, payload))
                results.append(None)
                cache = self.path(operation, payload)
                if cache in keys:
                    original = keys[cache]
                    aliases[original].append(index)
                    if results[original] is not None:
                        ready.append((index, results[original]))
                else:
                    keys[cache], aliases[index] = index, [index]
                    if cache.exists():
                        value = self.validate(operation, payload, json.loads(cache.read_text(encoding="utf-8")))
                        emit(self.model, "cached", operation=operation)
                        ready.append((index, value))
                        logger.info("task=%s operation=%s cached=true", self.common["task_id"], operation)
                    else:
                        missing.append(index)
            if priority:
                pending.extendleft(reversed(missing))
            else:
                pending.extend(missing)

        def publish(index, value):
            for copy in list(aliases.get(index, [index])):
                results[copy] = value
                # 回调只在协调流程执行；就绪的后续请求优先领取同一并发名额。
                register(completed(jobs[copy], value) or [], priority=True)

        # 先核对整组已有缓存，避免发现损坏缓存前已经发出新请求。
        register(initial)
        stopped = asyncio.create_task(self.stop.wait())

        def collect():
            nonlocal failure
            for future in sorted([f for f in active if f.done()], key=lambda f: active[f][0]):
                index, tick = active.pop(future)
                if future.cancelled():
                    continue
                operation, payload = jobs[index]
                try:
                    value = self.validate(operation, payload, future.result())
                    emit(self.model, "saving", operation=operation)
                    atomic_json(self.path(operation, payload), value)
                    if operation == "chapters":
                        save_chapters(payload, value)
                    publish(index, value)
                    emit(self.model, "saved", operation=operation)
                    logger.info("task=%s operation=%s job=%s cached=false duration=%.3f estimated_input=%s input_unit=%s",
                                self.common["task_id"], operation, self.path(operation, payload).stem, time.monotonic() - tick,
                                self.model.input_characters(operation, payload),
                                "characters")
                except Exception as exc:
                    if failure is None:
                        failure = exc
                        self.failed_job = jobs[index]
                    elif self.model.diagnostics:
                        self.model.diagnostics.record("model_parallel_failed", "另一项模型调用失败。", exc,
                                                      task_id=self.common["task_id"], operation=operation)

        try:
            while active or pending or ready:
                collect_partials()
                try:
                    self.check()
                except TaskPaused as exc:
                    failure = exc
                while failure is None and ready:
                    index, value = ready.popleft()
                    try:
                        publish(index, value)
                    except Exception as exc:
                        failure, self.failed_job = exc, jobs[index]
                while failure is None and pending and len(active) < self.settings.llm_section_concurrency:
                    index = pending.popleft()
                    operation, payload = jobs[index]
                    try:
                        if not self.fits(operation, payload):
                            raise AppError("model_input_budget", "模型输入超过预算，未发出请求。")
                        self.check()
                    except (TaskPaused, AppError) as exc:
                        failure = exc
                        break
                    validate = lambda value, op=operation, data=payload: self.validate(op, data, value)
                    if operation == "chapters":
                        def chapter(draft, data=payload):
                            request = next(c for c in data["chapters"] if c["chapter_id"] == draft["chapter_id"])
                            return self.validate("chapters", {"chapters": [request]}, {"chapters": [draft]})["chapters"][0]
                        # 回调只排入待保存章节，返回的 Future 由协调循环写盘后确认。
                        def accept(draft, data=payload):
                            saved = asyncio.get_running_loop().create_future()
                            partials.append((data, draft, saved))
                            partial_ready.set()
                            return saved
                        validate = ChapterCallbacks(validate, chapter, accept)
                    chapter_ids = ([item["chapter_id"] for item in payload["chapters"]] if operation == "chapters"
                                   else [payload["chapter"]["chapter_id"]] if operation == "extract" else [])
                    logger.info("task=%s operation=%s job=%s chapters=%s started=true", self.common["task_id"], operation,
                                self.path(operation, payload).stem, chapter_ids)
                    active[asyncio.create_task(self.model.generate(operation, payload, validate))] = (index, time.monotonic())
                if failure is None and active:
                    partial_wait = asyncio.create_task(partial_ready.wait())
                    try:
                        await asyncio.wait([*active, stopped, partial_wait], return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        partial_wait.cancel()
                        await asyncio.gather(partial_wait, return_exceptions=True)
                collect_partials()
                collect()
                if self.stop.is_set():
                    failure = TaskPaused()
                if failure is not None:
                    for future in active:
                        if not future.done():
                            future.cancel()
                    if active:
                        await asyncio.gather(*active, return_exceptions=True)
                    collect_partials()
                    collect()  # 取消边界上已经成功返回的结果仍保存。
                    self.check()  # 已提交的主动停止优先于失败。
                    raise failure
            self.check()
            return results[:len(initial)]
        finally:
            stopped.cancel()
            for future in active:
                future.cancel()
            await asyncio.gather(stopped, *active, return_exceptions=True)
            collect_partials()
            collect()  # Pipeline 取消生成协程时也收集已完成响应。

    def anchor_payload(self, ids=None):
        return [{"id": item["id"], "start_seconds": item["start_seconds"]}
                for item in self.anchors if ids is None or item["id"] in ids]

    async def planning(self):
        evidence = self.evidence
        self.prepared = None
        payload = {"anchors": self.anchor_payload(), "evidence": evidence, "context": self.boundary}
        if not self.fits("outline", payload):
            # 不能再按证据记录分段时，在段内提取；原文及独立上下文仍完整保留。
            chapter = {"chapter_id": f"C{self.chapter_offset + 1}", "title": "当前片段",
                       "core_question": "材料主要阐述了什么？", "topics": ["主要内容"], "context": self.boundary,
                       "previous_chapter_outline": self.previous_chapter_outline}
            material, previous, field, issues = evidence, None, "primary", []
            while True:
                make = lambda items: {"chapter": {**chapter, field: items, "context":
                    preceding_context(self.all_evidence, items[0]["start_seconds"])
                    if field == "primary" and items else self.boundary}}
                groups = self.batches("extract", material, make)
                values = await self.pool([("extract", make(group)) for group in groups])
                facts = [fact for value in values for fact in value["facts"]]
                issues.extend(issue for value in values for issue in value["issues"])
                text = "\n".join(fact["text"] for fact in facts)
                if not text or previous is not None and len(text) >= previous:
                    raise AppError("model_input_budget", "超长记录提取后仍无法放入规划范围，检查点已保留。")
                previous = len(text)
                payload["evidence"] = [{**self.anchors[0], "text": text}]
                payload["anchors"] = self.anchor_payload({self.anchors[0]["id"]})
                if self.fits("outline", payload):
                    self.prepared = {"facts": facts, "issues": issues}
                    break
                material, field = facts, "facts"
        outline = (await self.pool([("outline", payload)]))[0]
        self.content_type = outline["content_type"]
        return outline

    # 按递增时间边界分配唯一主要证据，前文只作为上下文补充。
    def assign(self, outline):
        source = {item["id"]: item for item in self.evidence}
        starts = [self.range_start, *[source[item["start_anchor_id"]]["start_seconds"] for item in outline["chapters"][1:]], self.range_end]
        if any(a >= b for a, b in zip(starts, starts[1:])):
            raise ValueError("chapter_time_ranges_must_increase")
        chapters = []
        for index, plan in enumerate(outline["chapters"]):
            primary = [item for item in self.evidence if starts[index] <= item["start_seconds"] < starts[index + 1]]
            if not primary:
                raise ValueError("chapter_has_no_primary_evidence")
            chapters.append({**plan, "chapter_id": f"C{self.chapter_offset + index + 1}", "start_seconds": starts[index],
                             "end_seconds": starts[index + 1], "primary": primary,
                             "context": preceding_context(self.all_evidence, starts[index]),
                             "previous_chapter_outline": {key: value for key, value in
                                 (outline["chapters"][index - 1] if index else self.previous_chapter_outline).items()
                                 if key in {"title", "core_question", "topics"}}})
        if getattr(self, "prepared", None):
            chapters[0].update(self.prepared)
        assert sum(len(chapter["primary"]) for chapter in chapters) == len(self.evidence)
        return chapters

    # 小章节直接合批，超预算章节先提取事实，并只补写缺失章节。
    async def writing(self, chapters, video_summary):
        drafts, small, jobs, rounds = {}, [], [], {}
        self.body_paths = {c["chapter_id"]: self.path("chapter", {"video_summary": video_summary, "chapter": c}) for c in chapters}
        for chapter in chapters:
            cache = self.body_path(chapter)
            if cache.exists():
                value = self.validate("chapters", {"chapters": [chapter]},
                                      {"chapters": [json.loads(cache.read_text(encoding="utf-8"))]})
                drafts[chapter["chapter_id"]] = value["chapters"][0]
                emit(self.model, "cached", operation="chapters")
                logger.info("task=%s operation=chapter chapter=%s cached=true", self.common["task_id"], chapter["chapter_id"])
        if drafts:
            self.update(f"summarizing:{len(drafts)}/{len(chapters)}")
        def payload(group):
            return {"video_summary": video_summary, "chapters": [{key: value for key, value in chapter.items()
                                   if key != "primary" or "facts" not in chapter} for chapter in group]}
        def extracting(chapter, requests, previous=None):
            rounds[chapter["chapter_id"]] = (chapter, [self.path(*job) for job in requests], {}, previous)
            return requests
        def received(job, value):
            if job[0] == "chapters":
                changed = False
                for draft in value["chapters"]:
                    changed |= draft["chapter_id"] not in drafts
                    drafts[draft["chapter_id"]] = draft
                if changed:
                    self.update(f"summarizing:{len(drafts)}/{len(chapters)}")
                return []
            identifier = job[1]["chapter"]["chapter_id"]
            state = rounds.get(identifier)
            key = self.path(*job)
            if state is None or key not in state[1]:
                return []
            chapter, paths, values, previous = state
            values[key] = value
            if any(path not in values for path in paths):
                return []
            rounds.pop(identifier)
            reduced = {key: value for key, value in chapter.items() if key != "primary"}
            reduced.update(facts=[fact for path in paths for fact in values[path]["facts"]],
                           issues=[*chapter.get("issues", []), *[issue for path in paths for issue in values[path]["issues"]]])
            estimate = self.model.input_characters("chapters", self.input_payload("chapters", payload([reduced])))
            if previous is not None and estimate >= previous:
                raise AppError("model_input_budget", "章节信息仍超过预算，已保存检查点，请增大输入预算后重试。")
            if self.fits("chapters", payload([reduced])):
                return [("chapters", payload([reduced]))]
            if not reduced["facts"]:
                raise AppError("model_input_budget", "章节信息仍超过预算，已保存检查点，请增大输入预算后重试。")
            make = lambda items: {"video_summary": video_summary, "chapter": {**reduced, "facts": items}}
            groups = self.batches("extract", reduced["facts"], make)
            return extracting(reduced, [("extract", make(group)) for group in groups], estimate)
        def flush():
            if small:
                jobs.append(("chapters", payload(list(small))))
                small.clear()
        for chapter in chapters:
            if chapter["chapter_id"] in drafts:
                continue
            if self.fits("chapters", payload([chapter])):
                if small and (len(small) == MAX_BATCH_CHAPTERS or not self.fits("chapters", payload([*small, chapter])) or
                              not self.output_fits([*small, chapter])):
                    flush()
                small.append(chapter)
            else:
                flush()
                make = lambda items, c=chapter: {"video_summary": video_summary, "chapter": {**c, "primary": items, "context":
                    preceding_context(self.all_evidence, items[0]["start_seconds"]) if items else c["context"]}}
                groups = self.batches("extract", chapter["primary"], make)
                requests = [("extract", make(group)) for group in groups]
                jobs.extend(extracting(chapter, requests))
        flush()
        def expand(job):
            operation, data = job
            if operation == "chapters":
                group = [c for c in data["chapters"] if c["chapter_id"] not in drafts]
                if not group:
                    return []
                if self.path(*job).with_suffix(".split.json").exists() and len(group) > 1:
                    middle = len(group) // 2
                    return [item for part in (group[:middle], group[middle:])
                            for item in expand((operation, payload(part)))]
                return [(operation, payload(group))]
            return [job]
        while True:
            try:
                await self.pool([item for job in jobs for item in expand(job)], received)
                break
            except AppError as exc:
                failed = self.failed_job
                if exc.code != "model_output_truncated" or not failed or failed[0] != "chapters" or len(failed[1]["chapters"]) < 2:
                    raise
                atomic_json(self.path(*failed).with_suffix(".split.json"), {"reason": exc.code})
                # 保留已完成章节；只缩小实际截断的批次，不改正文长度。
                jobs = [failed, *[job for job in jobs if job != failed]]
        return [drafts[chapter["chapter_id"]] for chapter in chapters]

    def output_fits(self, chapters):
        # 仅用于合批调度；不向模型施加正文长度要求。按原文保守估算并预留20%输出余量。
        material = [{key: chapter[key] for key in ("primary", "facts", "issues") if key in chapter}
                    for chapter in chapters]
        estimate = len(json.dumps(material, ensure_ascii=False).encode("utf-8")) + 512 * len(chapters)
        return estimate <= self.settings.llm_output_tokens["chapters"] * OUTPUT_BUDGET_RATIO

    def diagnostic(self, code, message, **details):
        logger.warning("task=%s code=%s %s details=%s", self.common["task_id"], code, message, details)
        if self.model.diagnostics:
            self.model.diagnostics.record(code, message, task_id=self.common["task_id"], **details)

    # 核对存疑项的证据归属和连续原文，必要时单独修正引文。
    async def source_issues(self, chapters, drafts):
        issues, repairs = [], []
        for chapter, draft in zip(chapters, drafts):
            primary = {item["id"] for item in chapter["primary"]}
            allowed = primary | {item["id"] for item in chapter["context"]}
            for raw in draft["issues"]:
                try:
                    issue = SourceIssue.model_validate(raw).model_dump()
                except ValueError:
                    self.diagnostic("invalid_source_issue", "忽略无法解析的辅助存疑项。", chapter_id=chapter["chapter_id"])
                    continue
                ids = [i for i in issue["evidence_ids"] if i in allowed]
                if set(ids) != set(issue["evidence_ids"]) or not primary.intersection(ids):
                    self.diagnostic("invalid_issue_reference", "存疑来源无效，正文检查点保留。", chapter_id=chapter["chapter_id"])
                    if not issue["critical"]:
                        continue
                    # 仅保留章节层面的疑问；不把错误引文伪装成精确引用。
                    ids, issue["quote"] = list(primary), None
                issue.update(evidence_ids=ids, chapter_id=chapter["chapter_id"], id=f"{chapter['chapter_id']}-I{len(issues) + 1}")
                issues.append(issue)
                quote = issue.get("quote")
                if quote and not any(quote in self.original[i]["text"] for i in ids):
                    repairs.append({"chapter_id": chapter["chapter_id"], "block_index": len(issues) - 1,
                                    "quote": quote, "reason": issue["reason"], "evidence_ids": ids})
        if repairs:
            def make(group):
                ids = list(dict.fromkeys(i for item in group for i in item["evidence_ids"]))
                return {"repairs": group, "sources": [{"id": i, "kind": self.original[i]["kind"],
                                                       "text": self.original[i]["text"]} for i in ids]}
            try:
                groups = self.batches("issue_quotes", repairs, make)
                def received(job, result):
                    for repair in result["repairs"]:
                        issues[repair["block_index"]]["quote"] = repair["quote"]
                await self.pool([("issue_quotes", make(group)) for group in groups], received)
            except (AppError, ValueError) as exc:
                if isinstance(exc, AppError) and exc.code == "model_input_budget":
                    raise
                self.diagnostic("issue_quote_repair_failed", "辅助引文修正未完成，保留正文并降级处理存疑项。",
                                error_code=getattr(exc, "code", "invalid_model_output"))
        cleaned, seen = [], set()
        for issue in issues:
            quote = issue.get("quote")
            invalid = quote and not any(quote in self.original[i]["text"] for i in issue["evidence_ids"])
            if invalid or not quote:
                if not issue["critical"]:
                    continue
                issue["quote"] = None
            key = (issue["chapter_id"], tuple(issue["evidence_ids"]), issue["quote"], issue["reason"])
            if key not in seen:
                seen.add(key)
                issue["evidence_ids"] = [self.original[i]["id"] for i in issue["evidence_ids"]]
                cleaned.append(issue)
        return cleaned

    def assemble(self, chapters, drafts, issues):
        result = [{"chapter_id": chapter["chapter_id"], "title": chapter["title"], "markdown": draft["markdown"],
                   "evidence_ids": [self.original[i["id"]]["id"] for i in chapter["primary"]],
                   "start_seconds": chapter["start_seconds"], "end_seconds": chapter["end_seconds"]}
                  for chapter, draft in zip(chapters, drafts)]
        evidence = {item["id"]: item for item in self.original.values()}
        return ground_chapters(result, issues, evidence, self.duration, start=self.range_start, end=self.range_end)

    async def generate_segment(self, preview=False):
        for stage in ("planning", "summarizing"):
            self.phase_update(stage)
            self.update(stage if stage == "planning" else f"summarizing:0/{len(chapters)}")
            tick = time.monotonic()
            try:
                if stage == "planning":
                    outline = await self.planning()
                    self.outline_digest = digest(outline)
                    chapters = self.assign(outline)
                    if preview:
                        self.outline_ready(outline["video_summary"])
                else:
                    drafts = await self.writing(chapters, outline["video_summary"])
                    self.phase_update("issues")
                    issues = await self.source_issues(chapters, drafts)
                    body = self.assemble(chapters, drafts, issues)
            finally:
                elapsed = round(time.monotonic() - tick, 3)
                self.timings[stage] = round(self.timings.get(stage, 0) + elapsed, 3)
                logger.info("task=%s stage=%s duration=%.3f", self.common["task_id"], stage, elapsed)
        return {"outline": outline, "drafts": drafts, "chapters": body, "source_issues": issues}

    def overview_payload(self, records):
        return {"video_summaries": [record["outline"]["video_summary"] for record in records]}

    async def overview(self, records):
        payload = self.overview_payload(records)
        make = lambda items: {"video_summaries": [item["text"] for item in items]}
        while not self.fits("overview", payload):
            previous = self.model.input_characters("overview", self.input_payload("overview", payload))
            groups = self.batches("overview", [{"text": summary} for summary in payload["video_summaries"]], make)
            values = await self.pool([("overview", make(group)) for group in groups])
            payload = {"video_summaries": ["\n\n".join(value["overview_paragraphs"]) for value in values]}
            if self.model.input_characters("overview", self.input_payload("overview", payload)) >= previous:
                raise AppError("model_input_budget", "视频主题导览汇总未收敛，已保存全部正文和成功检查点。")
        return (await self.pool([("overview", payload)]))[0]

    # 按时间分段接续已保存记录，发布分段正文后再汇总整体导览。
    async def run(self):
        self.check()
        self.content_type = None
        all_evidence, base = self.all_evidence, self.base_common
        self.common = base
        measure = outline_measure(self.model, self.video_title)
        segmented, segments = split_segments(all_evidence, self.duration, self.settings.llm_input_text_limit, measure)
        logger.info("task=%s source_characters=%s input_characters=%s segments=%s",
                    base["task_id"], sum(len(item["text"]) for item in all_evidence),
                    outline_text_length({"evidence": all_evidence}), len(segments))
        manifest = [{**{key: segment[key] for key in ("index", "start_seconds", "end_seconds")},
                     "evidence_ids": [item["id"] for item in segment["primary"]]} for segment in segments]
        manifest_path = self.path("segments", manifest)
        if manifest_path.exists():
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise AppError("invalid_checkpoint", "分段检查点与原文不一致，请重新生成笔记。")
        else:
            atomic_json(manifest_path, manifest)
        generation_key = digest(base)
        records, body, issues = [], [], []
        self.previous_chapter_outline = {}
        guides = []

        def progress(segment, completed, phase):
            if segmented:
                self.segment_update({"generation_key": generation_key, "current": segment["index"],
                    "completed": completed, "total": len(segments), "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"], "phase": phase})

        for segment, description in zip(segments, manifest):
            self.check()
            self.range_start, self.range_end = segment["start_seconds"], segment["end_seconds"]
            self.chapter_offset = len(body)
            self.evidence = segment["primary"]
            self.anchors = deduplicate_ocr({"evidence": self.evidence})["evidence"]
            self.boundary = preceding_context(all_evidence, self.range_start)
            self.common = {**base, "segment": description, "context": digest(self.boundary),
                           "previous_outline": digest(self.previous_chapter_outline)}
            self.outline_digest = None
            cache = self.path("segment", description)
            self.phase_update = lambda phase, s=segment: progress(s, s["index"] - 1, phase)
            if cache.exists():
                record = json.loads(cache.read_text(encoding="utf-8"))
                record["outline"] = self.validate("outline", self.input_payload("outline", {"anchors": self.anchor_payload()}), record["outline"])
                self.content_type = record["outline"]["content_type"]
                self.validate("chapters", {"chapters": record["chapters"]}, {"chapters": record["drafts"]})
                expected_ids = [f"C{self.chapter_offset + index + 1}" for index in range(len(record["chapters"]))]
                if ([chapter["chapter_id"] for chapter in record["chapters"]] != expected_ids or
                        any(chapter["markdown"] != draft["markdown"] for chapter, draft in zip(record["chapters"], record["drafts"]))):
                    raise AppError("invalid_checkpoint", "片段正文、编号或衔接检查点不一致。")
                ground_chapters(record["chapters"], record["source_issues"],
                                {item["id"]: item for item in self.original.values()}, self.duration,
                                start=self.range_start, end=self.range_end)
                expected = [self.original[item["id"]]["id"] for item in self.evidence]
                if [i for chapter in record["chapters"] for i in chapter["evidence_ids"]] != expected:
                    raise AppError("invalid_checkpoint", "片段检查点的原文归属不一致。")
                if not segmented:
                    self.outline_ready(record["outline"]["video_summary"])
                logger.info("task=%s segment=%s cached=true", base["task_id"], segment["index"])
                emit(self.model, "cached", message=f"复用已完成的第 {segment['index']} 段笔记")
            else:
                record = await self.generate_segment(preview=not segmented)
                atomic_json(cache, record)
            records.append(record)
            body.extend(record["chapters"])
            issues.extend(record["source_issues"])
            self.previous_chapter_outline = {key: value for key, value in record["outline"]["chapters"][-1].items()
                                             if key in {"title", "core_question", "topics"}}
            if segmented:
                guides.append({"index": segment["index"], "start_seconds": self.range_start,
                               "end_seconds": self.range_end, "first_chapter_id": record["chapters"][0]["chapter_id"],
                               "summary": record["outline"]["video_summary"]})
            progress(segment, segment["index"], "completed")
            self.publish({"format_version": FORMAT_VERSION, "note_style": "detailed", "content_type": self.content_type, "title": "", "tags": [], "overview_paragraphs": [],
                          "chapters": list(body), "segment_guides": list(guides), "source_issues": list(issues), "complete": False,
                          "covered_until_seconds": self.range_end, "completed_segments": segment["index"],
                          "total_segments": len(segments), "generation_key": generation_key,
                          "evidence_ids": list(dict.fromkeys(i for chapter in body for i in chapter["evidence_ids"]))})
        self.check()
        self.common, self.evidence = base, all_evidence
        self.boundary = []
        self.outline_digest = digest([record["outline"] for record in records])
        progress(segments[-1], len(segments), "overview")
        self.update("finalizing")
        tick = time.monotonic()
        try:
            metadata = await self.overview(records)
        finally:
            self.timings["finalizing"] = round(time.monotonic() - tick, 3)
        return {"format_version": FORMAT_VERSION, "note_style": "detailed", "content_type": self.content_type, **metadata, "chapters": body, "source_issues": issues,
                "segment_guides": guides,
                "complete": True, "covered_until_seconds": self.duration, "completed_segments": len(segments),
                "total_segments": len(segments), "generation_key": generation_key}
