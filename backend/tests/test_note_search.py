from copy import deepcopy
import pytest
from sqlalchemy import event
from app.db import Video, Task, Document, DocumentTask


def seed(app, kind="video", *, identifier="file", task_id="saved", date="2026-10-20", status="succeeded",
         style="detailed", result=None):
    result = deepcopy(result) if result is not None else {
        "format_version": 4 if kind == "video" else 1, "complete": True, "note_style": style,
        "title": "生物化学", "overview_paragraphs": ["课程介绍"], "tags": ["不搜标签"],
        "chapters": [{"chapter_id": "C1", "title": "催化原理", "markdown": "酶的活性受到竞争性抑制影响。"}],
        "source_issues": [{"reason": "不搜存疑"}],
    }
    with app.state.sessions.begin() as session:
        model, task = (Video, Task) if kind == "video" else (Document, DocumentTask)
        if not session.get(model, identifier):
            fields = {"duration": 120, "video_codec": "h264"} if kind == "video" else {
                "format": "txt", "unit_count": 1, "character_count": 10}
            session.add(model(id=identifier, fingerprint=identifier, filename=identifier + ".txt", size_bytes=10, **fields))
            session.flush()
        session.add(task(id=task_id, **{("video_id" if kind == "video" else "document_id"): identifier},
                         status=status, updated_at=date, config={"secret": "never-public"}, result=result,
                         **({"completed_at": date} if kind == "document" else {})))
    return result


def search(client, query, page=1):
    response = client.get("/api/v1/notes/search", params={"q": query, "page": page})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
def test_search_latest_complete_note_without_model_or_source_reads(app, client, kind, style, monkeypatch):
    seed(app, kind, task_id="old", date="2026-10-19", style=style)
    latest = seed(app, kind, style=style)
    seed(app, kind, task_id="draft", date="2026-10-22", status="paused",
         result={**latest, "complete": False, "title": "草稿独有"})
    seed(app, kind, task_id="failed", date="2026-10-23", status="failed", result=latest)
    for name in ("run",):
        monkeypatch.setattr(app.state.pipeline, name, lambda *a, **kw: pytest.fail("search invoked generation"))
    result = search(client, "酶 抑制 酶")
    assert result["total"] == 1
    hit = result["items"][0]
    assert (hit["kind"], hit["task_id"], hit["chapter_id"], hit["area"]) == (kind, "saved", "C1", "chapter")
    assert hit["snippet"] == "酶的活性受到竞争性抑制影响。"
    assert hit["snippet_highlights"] == [[0, 1], [9, 11]]
    assert "never-public" not in str(result) and "config" not in hit
    assert search(client, "草稿独有")["total"] == 0
    assert search(client, "不搜存疑")["total"] == search(client, "不搜标签")["total"] == 0
    with app.state.sessions() as session:
        task = session.get(Task if kind == "video" else DocumentTask, "saved")
        assert task.result == latest and task.config == {"secret": "never-public"}


@pytest.mark.parametrize("kind", ["video", "document"])
def test_select_newest_valid_before_matching_not_newest_matching(app, client, kind):
    old = seed(app, kind, task_id="old", date="2026-10-19")
    seed(app, kind, result={**old, "chapters": [{"chapter_id": "C2", "title": "新笔记", "markdown": "更新内容"}]})
    seed(app, kind, task_id="empty", date="2026-10-21", result={**old, "chapters": []})
    assert search(client, "抑制")["total"] == 0
    assert search(client, "更新内容")["items"][0]["task_id"] == "saved"
    with app.state.sessions.begin() as session:
        session.get(Task if kind == "video" else DocumentTask, "saved").result = None
    assert search(client, "抑制")["items"][0]["task_id"] == "old"


def test_markdown_visible_content_only_with_literals_code_math_and_unicode(app, client):
    note = seed(app)
    note["chapters"][0]["markdown"] = (
        '😀 [可见说明](https://secret-link.invalid/不搜网址) <span>HTML正文</span>\n\n'
        '<!-- 不搜注释 -->\n\n```python\nVmax = "100%_.*"\n```\n\n$Km + Vmax$')
    with app.state.sessions.begin() as session:
        session.get(Task, "saved").result = note
    for query in ("可见说明", "HTML正文", "vMaX km", "100%_.*"):
        assert search(client, query)["total"] == 1
    for query in ("不搜网址", "secret-link", "不搜注释", "<span>", "python"):
        assert search(client, query)["total"] == 0
    hit = search(client, "可见说明")["items"][0]
    encoded = hit["snippet"].encode("utf-16-le")
    assert [encoded[start*2:end*2].decode("utf-16-le") for start, end in hit["snippet_highlights"]] == ["可见说明"]


def test_keywords_must_share_unit_and_order_is_stable(app, client):
    note = seed(app)
    note.update(title="概念甲", overview_paragraphs=["概览乙"], chapters=[
        {"chapter_id": "C1", "title": "甲", "markdown": "普通正文"},
        {"chapter_id": "C2", "title": "其他", "markdown": "乙"},
        {"chapter_id": "C3", "title": "甲", "markdown": "乙"},
    ])
    with app.state.sessions.begin() as session:
        session.get(Task, "saved").result = note
    hits = search(client, "甲 乙")["items"]
    assert [hit["chapter_id"] for hit in hits] == [None, "C3"]
    assert search(client, "甲 普通")["total"] == 1


def test_pagination_renames_deletes_and_batch_queries(app, client):
    for index in range(24):
        seed(app, identifier=f"f{index:02}", task_id=f"t{index:02}")
    engine = app.state.sessions.kw["bind"]
    statements = []
    def track(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", track)
    try:
        first, second = search(client, "抑制"), search(client, "抑制", 2)
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert first["total"] == second["total"] == 24
    assert len(first["items"]) == 20 and len(second["items"]) == 4
    assert len({hit["task_id"] for hit in first["items"] + second["items"]}) == 24
    assert len(statements) == 4 and all(statement.lstrip().startswith("SELECT") for statement in statements)
    with app.state.sessions.begin() as session:
        session.get(Video, "f23").filename = "已重命名.mp4"
        session.delete(session.get(Task, "t22"))
        session.flush()
        session.delete(session.get(Video, "f22"))
    assert search(client, "抑制")["items"][0]["filename"] == "已重命名.mp4"
    assert search(client, "抑制")["total"] == 23


def test_search_input_and_empty_database(client):
    assert search(client, " \t\n")["items"] == []
    assert search(client, "不存在")["total"] == 0
    assert client.get("/api/v1/notes/search", params={"q": "a" * 201}).status_code == 422
    assert client.get("/api/v1/notes/search", params={"page": 0}).status_code == 422


def test_title_priority_then_newest_and_chapter_order(app, client):
    note = seed(app, identifier="old", task_id="old", date="2026-10-18")
    note["chapters"] = [
        {"chapter_id": "C1", "title": "抑制类型", "markdown": "第一章正文"},
        {"chapter_id": "C2", "title": "抑制机制", "markdown": "第二章正文"},
    ]
    with app.state.sessions.begin() as session:
        session.get(Task, "old").result = note
    seed(app, identifier="new", task_id="new", date="2026-10-24")
    assert [(hit["task_id"], hit["chapter_id"]) for hit in search(client, "抑制")["items"]] == [
        ("old", "C1"), ("old", "C2"), ("new", "C1")]
    note["chapters"][0].update(title="一般条件", markdown="正文说明抑制作用")
    with app.state.sessions.begin() as session:
        session.get(Task, "old").result = note
    assert [hit["chapter_id"] for hit in search(client, "抑制")["items"][:2]] == ["C1", "C2"]


def test_existing_domain_version_order_and_video_complete_default(app, client):
    old = seed(app, "document", task_id="z", date="2026-10-18")
    seed(app, "document", task_id="a", date="2026-10-20", result={**old, "title": "最新文档"})
    with app.state.sessions.begin() as session:
        session.get(DocumentTask, "z").updated_at = "2026-10-25"
    assert search(client, "最新文档")["items"][0]["task_id"] == "a"
    video = seed(app, "video", identifier="video", task_id="video-z")
    video.pop("complete")
    with app.state.sessions.begin() as session:
        session.get(Task, "video-z").result = video
    seed(app, "video", identifier="video", task_id="video-a", result={**video, "title": "旧序笔记"})
    assert search(client, "旧序笔记")["total"] == 0
    assert any(hit["task_id"] == "video-z" for hit in search(client, "抑制")["items"])


def test_long_snippet_is_near_first_hit_and_overlapping_highlights_merge(app, client):
    note = seed(app)
    note["chapters"][0]["markdown"] = "前文" * 300 + "酶抑制原理" + "后文" * 300
    with app.state.sessions.begin() as session:
        session.get(Task, "saved").result = note
    hit = search(client, "抑制 酶抑制")["items"][0]
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    assert len(hit["snippet"]) == 202
    assert [hit["snippet"][start:end] for start, end in hit["snippet_highlights"]] == ["酶抑制"]


@pytest.mark.parametrize("kind", ["video", "document"])
def test_filename_matches_once_highlights_and_uses_current_name(app, client, kind):
    seed(app, kind)
    model, task = (Video, Task) if kind == "video" else (Document, DocumentTask)
    with app.state.sessions.begin() as session:
        session.get(model, "file").filename = "😀酶动力学 Lecture.txt"
    hits = search(client, "LECTURE 酶动力学")["items"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["area"] == "overview" and hit["chapter_id"] is None and hit["task_id"] == "saved"
    text = hit["filename"].encode("utf-16-le")
    assert [text[start*2:end*2].decode("utf-16-le") for start, end in hit["filename_highlights"]] == ["酶动力学", "Lecture"]
    assert search(client, "lecture 课程")["total"] == 1
    with app.state.sessions.begin() as session:
        session.get(model, "file").filename = "新文件名.txt"
    assert search(client, "lecture")["total"] == 0
    assert search(client, "新文件名")["total"] == 1
    with app.state.sessions.begin() as session:
        session.get(task, "saved").status = "failed"
    assert search(client, "新文件名")["total"] == 0
