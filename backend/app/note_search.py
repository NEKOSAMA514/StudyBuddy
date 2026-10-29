"""只读搜索每份资料最新完整笔记中的关键词。"""
import re
from sqlalchemy import select
from .db import Video, Task, Document, DocumentTask
from .analysis.markdown import markdown_text
from .videos.schemas import current_notes

PAGE_SIZE = 20


def highlights(text, patterns):
    spans = sorted(match.span() for pattern in patterns for match in pattern.finditer(text))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    # 浏览器字符串偏移按 UTF-16 码元计数，需计入代理对占用的位置。
    return [[len(text[:start].encode("utf-16-le")) // 2, len(text[:end].encode("utf-16-le")) // 2]
            for start, end in merged]


def snippet(text, patterns):
    text = " ".join(text.split())
    first = min((match.start() for pattern in patterns if (match := pattern.search(text))), default=0)
    start = max(0, first - 50)
    end = min(len(text), start + 200)
    value = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    return value, highlights(value, patterns)


# 只搜索每份资料最新完整笔记，按关键词共同命中构造高亮和摘要。
def search_notes(sessions, query, page):
    keywords = list(dict.fromkeys(word.lower() for word in query.split()))
    response = {"items": [], "total": 0, "page": page, "page_size": PAGE_SIZE}
    if not keywords:
        return response
    patterns = [re.compile(re.escape(word), re.I) for word in keywords]
    hits = []
    with sessions() as session:
        for kind, file, task, foreign_key, date in (
            ("video", Video, Task, Task.video_id, Task.updated_at),
            ("document", Document, DocumentTask, DocumentTask.document_id, DocumentTask.completed_at),
        ):
            rows = session.execute(select(task.id, foreign_key, file.filename, task.result, date)
                                   .join(file, file.id == foreign_key)
                                   .where(task.status == "succeeded", task.result.is_not(None))
                                   .order_by(date.desc(), task.id.desc()))
            seen = set()
            for task_id, file_id, filename, result, saved_at in rows:
                valid = (current_notes(result) and result.get("complete", True)) if kind == "video" else (
                    result and result.get("complete") and result.get("chapters"))
                if file_id in seen or not valid:
                    continue
                seen.add(file_id)
                units = [("overview", None, result["title"], "\n\n".join(result.get("overview_paragraphs", [])))]
                units.extend(("chapter", chapter["chapter_id"], chapter["title"],
                              markdown_text(chapter["markdown"], include_link_urls=False))
                             for chapter in result["chapters"])
                matches = []
                for index, (area, chapter_id, title, body) in enumerate(units):
                    text = (filename + "\n" if area == "overview" else "") + title + "\n" + body
                    if not all(pattern.search(text) for pattern in patterns):
                        continue
                    excerpt, ranges = snippet(body, patterns)
                    item = {"kind": kind, "file_id": file_id, "filename": filename, "task_id": task_id,
                            "filename_highlights": highlights(filename, patterns),
                            "note_title": result["title"], "area": area, "chapter_id": chapter_id,
                            "title": title, "title_highlights": highlights(title, patterns),
                            "snippet": excerpt, "snippet_highlights": ranges}
                    title_hit = bool(item["title_highlights"]) or area == "overview" and bool(item["filename_highlights"])
                    matches.append((title_hit, saved_at or "", task_id, kind, file_id, -index, item))
                # 按各文件的最佳命中排序，同一文件内部保留阅读顺序。
                title_match = any(match[0] for match in matches)
                hits.extend((title_match, *match[1:]) for match in matches)
    hits.sort(key=lambda hit: hit[:-1], reverse=True)
    response.update(total=len(hits), items=[hit[-1] for hit in hits[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]])
    return response


def search_router(sessions):
    from fastapi import APIRouter, Query
    router = APIRouter()

    @router.get("/api/v1/notes/search")
    def search(q: str = Query(default="", max_length=200), page: int = Query(default=1, ge=1)):
        return search_notes(sessions, q, page)

    return router
