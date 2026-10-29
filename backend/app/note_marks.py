"""绑定笔记版本的手动重点标记，不修改生成正文或任务快照。"""
import re
from typing import Literal
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import delete, select
from .db import Task, DocumentTask, Video, Document, NoteMark
from .errors import AppError
from .analysis.markdown import normalize_math, text_parser, inline_text, markdown_text
from .videos.schemas import current_notes


class MarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str = Field(min_length=1, max_length=128)
    heading_index: int = Field(ge=0, strict=True)
    marked: StrictBool


def clear_marks(session, kind, task_ids):
    session.execute(delete(NoteMark).where(NoteMark.kind == kind, NoteMark.task_id.in_(task_ids)))


def heading_count(markdown):
    return len(section_spans(markdown)[1])


# 按 Markdown 实际二级标题确定小节范围，避免把脚注标题计为可标记内容。
def section_spans(markdown):
    lines = normalize_math(markdown).splitlines(keepends=True)
    tokens = text_parser.parse("".join(lines))
    headings = [(index, token) for index, token in enumerate(tokens)
                if token.type == "heading_open" and token.tag in {"h1", "h2"}]
    return lines, [(inline_text(tokens[index + 1].children, include_link_urls=False), token.map[1],
                    headings[position + 1][1].map[0] if position + 1 < len(headings) else len(lines))
                   for position, (index, token) in enumerate(headings) if token.tag == "h2"]


# 批量读取仍保留的成功版本并生成重点摘要，不复制或修改笔记正文。
def marked_notes(sessions, page):
    items = []
    with sessions() as session:
        marks = {}
        for mark in session.scalars(select(NoteMark)):
            marks.setdefault((mark.kind, mark.task_id), {}).setdefault(mark.chapter_id, set()).add(mark.heading_index)
        for kind, file, task, file_key, date in (
                ("video", Video, Task, Task.video_id, Task.updated_at),
                ("document", Document, DocumentTask, DocumentTask.document_id, DocumentTask.completed_at)):
            rows = session.execute(select(task.id, file.id, file.filename, task.result, date)
                                   .join(file, file.id == file_key)
                                   .where(task.status == "succeeded", task.result.is_not(None))
                                   .order_by(date.desc(), task.id.desc()))
            versions = {}
            for task_id, file_id, filename, result, saved_at in rows:
                valid = current_notes(result) and result.get("complete", True) if kind == "video" else (
                    result and result.get("complete") and result.get("chapters"))
                if not valid:
                    continue
                version = versions[file_id] = versions.get(file_id, 0) + 1
                if version > 2 or (kind, task_id) not in marks:
                    continue
                concise = result.get("note_style") == "concise"
                for order, chapter in enumerate(result["chapters"]):
                    targets = marks[(kind, task_id)].get(chapter["chapter_id"], set())
                    if not targets:
                        continue
                    if concise:
                        lines, spans = section_spans(chapter["markdown"])
                        sections = [(index, title or f"小节 {index}", "".join(lines[start:end]))
                                    for index, (title, start, end) in enumerate(spans, 1) if index in targets]
                    else:
                        sections = [(0, chapter["title"], normalize_math(chapter["markdown"]))] if 0 in targets else []
                    for index, title, body in sections:
                        text = " ".join(markdown_text(body, include_link_urls=False).split())
                        sentence_end = re.search(r"[。；;]|(?<!\d)[:：]|[:：](?!\d)|\.(?=\s|$)", text)
                        item = {"kind": kind, "file_id": file_id, "filename": filename, "task_id": task_id,
                                "note_title": result["title"], "version": version, "chapter_id": chapter["chapter_id"],
                                "chapter_title": chapter["title"], "heading_index": index, "title": title,
                                "snippet": text[:sentence_end.end()] if sentence_end else text[:200] + ("…" if len(text) > 200 else "")}
                        items.append((saved_at or "", task_id, kind, file_id, -order, -index, item))
    items.sort(key=lambda item: item[:-1], reverse=True)
    return {"items": [item[-1] for item in items[(page - 1) * 20:page * 20]],
            "total": len(items), "page": page, "page_size": 20}


# 仅允许对当前保留的两份完整成功笔记读取或写入重点。
def readable_note(session, kind, identifier):
    model = Task if kind == "video" else DocumentTask
    task = session.get(model, identifier)
    if task is None:
        raise AppError("not_found", "笔记任务不存在。", 404)
    file_key = model.video_id if kind == "video" else model.document_id
    file_id = task.video_id if kind == "video" else task.document_id
    date = model.updated_at if kind == "video" else model.completed_at
    candidates = session.execute(select(model.id, model.result).where(file_key == file_id, model.status == "succeeded")
                                 .order_by(date.desc(), model.id.desc()))
    valid = 0
    for task_id, result in candidates:
        complete = current_notes(result) and result.get("complete", True) if kind == "video" else (
            result and result.get("complete") and result.get("chapters"))
        if complete:
            valid += 1
            if task_id == identifier and valid <= 2:
                return result
            if valid == 2:
                break
    raise AppError("notes_unavailable", "仅可为仍保留的成功完整笔记标记重点。", 409)


# 读取和幂等更新指定版本的重点，写事务内再次校验版本与目标。
def mark_router(sessions):
    router = APIRouter()

    @router.get("/api/v1/note-marks")
    def listing(page: int = Query(default=1, ge=1)):
        return marked_notes(sessions, page)

    @router.get("/api/v1/note-marks/{kind}/{task_id}")
    def read(kind: Literal["video", "document"], task_id: str):
        with sessions() as session:
            readable_note(session, kind, task_id)
            marks = session.scalars(select(NoteMark).where(NoteMark.kind == kind, NoteMark.task_id == task_id)
                                    .order_by(NoteMark.chapter_id, NoteMark.heading_index))
            return {"marks": [{"chapter_id": mark.chapter_id, "heading_index": mark.heading_index} for mark in marks]}

    @router.put("/api/v1/note-marks/{kind}/{task_id}")
    def write(kind: Literal["video", "document"], task_id: str, value: MarkInput):
        with sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = readable_note(session, kind, task_id)
            chapter = next((item for item in result["chapters"] if item["chapter_id"] == value.chapter_id), None)
            concise = result.get("note_style") == "concise"
            if chapter is None or not chapter.get("markdown", "").strip() or (not concise and value.heading_index != 0) or (
                    concise and not 1 <= value.heading_index <= heading_count(chapter["markdown"])):
                raise AppError("invalid_input", "重点标记的章节或小标题不存在。", 400)
            mark = session.get(NoteMark, (kind, task_id, value.chapter_id, value.heading_index))
            if value.marked and mark is None:
                session.add(NoteMark(kind=kind, task_id=task_id, chapter_id=value.chapter_id, heading_index=value.heading_index))
            elif not value.marked and mark is not None:
                session.delete(mark)
            return {"chapter_id": value.chapter_id, "heading_index": value.heading_index, "marked": value.marked}

    return router
