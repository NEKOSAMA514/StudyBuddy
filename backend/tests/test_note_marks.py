import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import pytest
from sqlalchemy import event, select
from app.db import Video, Document, Task, DocumentTask, NoteMark, database
from app.note_marks import heading_count, clear_marks

HEADINGS = json.loads((Path(__file__).parent / "fixtures/note-mark-headings.json").read_text(encoding="utf-8"))


def stored_note(app, kind, *, identifier="note", task_id="first", style="detailed", status="succeeded", date="2026-10-20"):
    result = {"format_version": 4 if kind == "video" else 1, "kind": kind, "note_style": style, "complete": True,
              "title": "测试笔记", "overview_paragraphs": [], "tags": [], "source_issues": [], "total_segments": 1,
              "chapters": [{"chapter_id": "C1", "title": "第一章", "markdown": "\n\n".join(case["markdown"] for case in HEADINGS)},
                           {"chapter_id": "C2", "title": "第二章", "markdown": "没有二级标题的正文。"}]}
    model, task_model = (Video, Task) if kind == "video" else (Document, DocumentTask)
    with app.state.sessions.begin() as session:
        if not session.get(model, identifier):
            fields = {"duration": 30, "video_codec": "h264"} if kind == "video" else {"format": "txt", "unit_count": 1, "character_count": 10}
            session.add(model(id=identifier, fingerprint=identifier, filename="测试.txt", size_bytes=10, **fields))
            session.flush()
        session.add(task_model(id=task_id, **{("video_id" if kind == "video" else "document_id"): identifier},
            status=status, config={"generation_key": "fixed", "note_style": style}, result=result, updated_at=date,
            **({"completed_at": date} if kind == "document" else {})))
    return result


def write(client, kind="video", task="first", chapter="C1", heading=0, marked=True):
    return client.put(f"/api/v1/note-marks/{kind}/{task}", json={"chapter_id": chapter, "heading_index": heading, "marked": marked})


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["detailed", "concise"])
def test_marks_persist_idempotently_without_modifying_notes_or_task(app, client, kind, style):
    original = stored_note(app, kind, style=style)
    model = Task if kind == "video" else DocumentTask
    with app.state.sessions() as session:
        row = session.get(model, "first")
        before = (deepcopy(row.config), row.updated_at, deepcopy(row.result), deepcopy(row.timings))
    heading = 1 if style == "concise" else 0
    endpoint = f"/api/v1/note-marks/{kind}/first"
    assert client.get(endpoint).json() == {"marks": []}
    for _ in range(2):
        assert write(client, kind, heading=heading).json() == {"chapter_id": "C1", "heading_index": heading, "marked": True}
    assert client.get(endpoint).json() == {"marks": [{"chapter_id": "C1", "heading_index": heading}]}
    engine, sessions = database(app.state.pipeline.get_settings().data_dir / "studybuddy.sqlite3")
    try:
        with sessions() as session:
            assert len(list(session.scalars(select(NoteMark)))) == 1
    finally:
        engine.dispose()
    with app.state.sessions() as session:
        row = session.get(model, "first")
        assert (row.config, row.updated_at, row.result, row.timings) == before
        assert row.result == original
    for _ in range(2):
        assert write(client, kind, heading=heading, marked=False).status_code == 200
    assert client.get(endpoint).json() == {"marks": []}


@pytest.mark.parametrize("case", HEADINGS, ids=lambda case: case["name"])
def test_markdown_heading_contract(case):
    assert heading_count(case["markdown"]) == case["count"]


@pytest.mark.parametrize("kind", ["video", "document"])
def test_concise_target_order_duplicate_titles_and_bounds(app, client, kind):
    stored_note(app, kind, style="concise")
    assert heading_count("\n\n".join(case["markdown"] for case in HEADINGS)) == 5
    for index in range(1, 6):
        assert write(client, kind, heading=index).status_code == 200
    for chapter, index in [("C1", 0), ("C1", 6), ("C2", 1), ("missing", 1)]:
        assert write(client, kind, chapter=chapter, heading=index).status_code == 400
    assert len(client.get(f"/api/v1/note-marks/{kind}/first").json()["marks"]) == 5


@pytest.mark.parametrize("kind", ["video", "document"])
def test_invalid_or_expired_targets_never_create_marks(app, client, kind):
    stored_note(app, kind)
    assert write(client, kind, heading=1).status_code == 400
    assert write(client, kind, chapter="missing").status_code == 400
    assert write(client, kind, heading=-1).status_code == 422
    assert write(client, kind, marked="true").status_code == 422
    model = Task if kind == "video" else DocumentTask
    with app.state.sessions.begin() as session:
        row = session.get(model, "first")
        row.result = {**row.result, "chapters": [{"chapter_id": "C1", "title": "空章", "markdown": " "}]}
    assert write(client, kind).status_code == 400
    for status, result in [("paused", "partial"), ("running", "partial"), ("failed", "full"), ("succeeded", None)]:
        with app.state.sessions.begin() as session:
            row = session.get(model, "first")
            row.status = status
            row.result = None if result is None else {**(row.result or {}), "complete": result == "full"}
        assert write(client, kind).status_code == 409
        assert client.get(f"/api/v1/note-marks/{kind}/first").status_code == 409
    assert write(client, kind, task="missing").status_code == 404
    with app.state.sessions() as session:
        assert list(session.scalars(select(NoteMark))) == []


@pytest.mark.parametrize("kind", ["video", "document"])
def test_new_versions_do_not_inherit_and_retention_is_atomic(app, client, kind):
    stored_note(app, kind)
    assert write(client, kind).status_code == 200
    stored_note(app, kind, task_id="second", date="2026-10-21")
    assert client.get(f"/api/v1/note-marks/{kind}/second").json() == {"marks": []}
    assert write(client, kind, task="second").status_code == 200
    stored_note(app, kind, task_id="third", date="2026-10-22")
    from app.videos.notes import retain_two_notes
    from app.documents.notes import retain_two
    retain = retain_two_notes if kind == "video" else retain_two
    model = Task if kind == "video" else DocumentTask
    with pytest.raises(RuntimeError, match="rollback"):
        with app.state.sessions.begin() as session:
            retain(session, "note")
            raise RuntimeError("rollback")
    with app.state.sessions() as session:
        assert session.get(model, "first").result is not None
        assert session.get(NoteMark, (kind, "first", "C1", 0)) is not None
    with app.state.sessions.begin() as session:
        retain(session, "note")
    assert client.get(f"/api/v1/note-marks/{kind}/first").status_code == 409
    with app.state.sessions() as session:
        assert session.get(NoteMark, (kind, "first", "C1", 0)) is None
        assert session.get(NoteMark, (kind, "second", "C1", 0)) is not None


@pytest.mark.parametrize("kind", ["video", "document"])
def test_delete_file_clears_marks_and_failed_delete_rolls_back(app, client, kind):
    stored_note(app, kind)
    assert write(client, kind).status_code == 200
    folder = app.state.pipeline.get_settings().data_dir / ("videos" if kind == "video" else "documents") / "note"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "original.txt").write_text("isolated original", encoding="utf-8")
    engine = app.state.sessions.kw["bind"]
    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("DELETE FROM note_marks"):
            raise RuntimeError("isolated deletion failure")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert client.delete(f"/api/v1/{'videos' if kind == 'video' else 'documents'}/note").status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert (folder / "original.txt").read_text(encoding="utf-8") == "isolated original"
    assert client.get(f"/api/v1/note-marks/{kind}/first").json()["marks"]
    assert client.delete(f"/api/v1/{'videos' if kind == 'video' else 'documents'}/note").status_code == 200
    assert not folder.exists()
    with app.state.sessions() as session:
        assert list(session.scalars(select(NoteMark))) == []


def test_write_failure_releases_transaction_and_local_origin_is_required(app, client):
    stored_note(app, "video")
    engine = app.state.sessions.kw["bind"]
    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO note_marks"):
            raise RuntimeError("isolated insert failure")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert write(client).status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert client.get("/api/v1/note-marks/video/first").json() == {"marks": []}
    assert write(client).status_code == 200
    assert client.put("/api/v1/note-marks/video/first", headers={"Origin": "https://untrusted.invalid"},
                      json={"chapter_id": "C1", "heading_index": 0, "marked": False}).status_code == 403


@pytest.mark.parametrize("kind", ["video", "document"])
def test_retirement_and_delayed_write_cannot_leave_orphan(app, client, kind):
    stored_note(app, kind)
    engine = app.state.sessions.kw["bind"]
    reached = threading.Event()
    model = Task if kind == "video" else DocumentTask
    def beginning(connection, cursor, statement, parameters, context, many):
        if statement == "BEGIN IMMEDIATE":
            reached.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with app.state.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            event.listen(engine, "before_cursor_execute", beginning)
            try:
                pending = executor.submit(write, client, kind)
                assert reached.wait(5)
                clear_marks(session, kind, ["first"])
                session.get(model, "first").result = None
            finally:
                event.remove(engine, "before_cursor_execute", beginning)
        assert pending.result(timeout=5).status_code == 409
    with app.state.sessions() as session:
        assert list(session.scalars(select(NoteMark))) == []


@pytest.mark.parametrize("kind", ["video", "document"])
@pytest.mark.parametrize("style", ["concise", "detailed"])
def test_center_lists_marked_sections_of_both_retained_versions(app, client, kind, style):
    stored_note(app, kind, style=style)
    heading = 1 if style == "concise" else 0
    assert write(client, kind, heading=heading).status_code == 200
    stored_note(app, kind, style=style, task_id="second", date="2026-10-21")
    assert write(client, kind, task="second", heading=heading).status_code == 200
    response = client.get("/api/v1/note-marks").json()
    assert response["total"] == 2
    assert [(item["task_id"], item["version"]) for item in response["items"]] == [("second", 1), ("first", 2)]
    for item in response["items"]:
        assert item["kind"] == kind and item["file_id"] == "note" and item["filename"] == "测试.txt"
        assert item["chapter_id"] == "C1" and item["heading_index"] == heading
        assert set(item) == {"kind", "file_id", "filename", "task_id", "note_title", "version", "chapter_id",
                             "chapter_title", "heading_index", "title", "snippet"}
        if style == "concise":
            assert item["title"] == "相同标题 00:05"
            assert item["snippet"] == "内容一。"
        else:
            assert item["title"] == "第一章" and item["snippet"] == "总标题 相同标题 00:05 内容一。"
    assert write(client, kind, heading=heading, marked=False).status_code == 200
    assert client.get("/api/v1/note-marks").json()["total"] == 1
    model = Task if kind == "video" else DocumentTask
    with app.state.sessions.begin() as session:
        row = session.get(model, "second")
        row.status = "paused"
    assert client.get("/api/v1/note-marks").json()["items"] == []


def test_center_heading_titles_snippets_order_and_batch_read(app, client):
    stored_note(app, "document", style="concise")
    for index in range(1, 6):
        assert write(client, "document", heading=index).status_code == 200
    stored_note(app, "video", task_id="video", date="2026-10-21")
    assert write(client, task="video").status_code == 200
    statements = []
    engine = app.state.sessions.kw["bind"]
    def reading(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", reading)
    try:
        response = client.get("/api/v1/note-marks").json()
    finally:
        event.remove(engine, "before_cursor_execute", reading)
    assert len(statements) == 3 and all("config" not in statement for statement in statements)
    assert response["items"][0]["kind"] == "video"
    items = response["items"][1:]
    assert [item["heading_index"] for item in items] == [1, 2, 3, 4, 5]
    assert [item["title"] for item in items] == ["相同标题 00:05", "相同标题 00:10", "Setext小标题", "引用小标题", "动力学 $K_m$"]
    assert [item["snippet"] for item in items[:2]] == ["内容一。", "内容二。"]
    assert all("https://" not in item["snippet"] and "<h2>" not in item["title"] for item in items)
    with app.state.sessions.begin() as session:
        session.get(Document, "note").filename = "重命名.md"
    assert client.get("/api/v1/note-marks").json()["items"][1]["filename"] == "重命名.md"


def test_center_pagination_unavailable_versions_and_no_orphans(app, client):
    stored_note(app, "video")
    model = Task
    with app.state.sessions.begin() as session:
        row = session.get(model, "first")
        row.result = {**row.result, "chapters": [{"chapter_id": f"C{index}", "title": f"章{index}", "markdown": "有效正文。"} for index in range(25)]}
    for index in range(25):
        assert write(client, chapter=f"C{index}").status_code == 200
    first, second = [client.get(f"/api/v1/note-marks?page={page}").json() for page in [1, 2]]
    assert (first["total"], first["page_size"], len(first["items"]), len(second["items"])) == (25, 20, 20, 5)
    assert [item["chapter_id"] for item in first["items"] + second["items"]] == [f"C{index}" for index in range(25)]
    assert client.get("/api/v1/note-marks?page=3").json()["items"] == []
    assert client.get("/api/v1/note-marks?page=0").status_code == 422
    stored_note(app, "video", task_id="second", date="2026-10-21")
    stored_note(app, "video", task_id="third", date="2026-10-22")
    assert client.get("/api/v1/note-marks").json()["items"] == []
    with app.state.sessions.begin() as session:
        session.add(NoteMark(kind="document", task_id="missing", chapter_id="C1", heading_index=0))
    assert client.get("/api/v1/note-marks").json()["items"] == []


@pytest.mark.parametrize("body, expected", [
    ("课程指出，银量法是沉淀滴定法中较重要的部分。后面的公式和细节不显示。", "课程指出，银量法是沉淀滴定法中较重要的部分。"),
    ("1.5秒完成操作。第二句不显示。", "1.5秒完成操作。"),
    ("This takes 1.5 seconds. The details follow.", "This takes 1.5 seconds."),
    ("00:05 介绍催化剂。后续说明。", "00:05 介绍催化剂。"),
    ("01:02:03 介绍反应速率。后续说明。", "01:02:03 介绍反应速率。"),
    ("00：05 介绍催化剂。后续说明。", "00：05 介绍催化剂。"),
    ("00:05 重点：后续说明。", "00:05 重点："),
    ("Key point: further explanation.", "Key point:"),
    ("先介绍概念；后续说明。", "先介绍概念；"),
    ("First concept; further explanation.", "First concept;"),
    ("长句" * 120 + "。后续。", "长句" * 120 + "。"),
    ("没有句号" * 60, ("没有句号" * 60)[:200] + "…"),
])
def test_center_preview_ends_at_first_sentence(app, client, body, expected):
    stored_note(app, "video")
    with app.state.sessions.begin() as session:
        row = session.get(Task, "first")
        row.result = {**row.result, "chapters": [{"chapter_id": "C1", "title": "章节", "markdown": body}]}
    assert write(client).status_code == 200
    assert client.get("/api/v1/note-marks").json()["items"][0]["snippet"] == expected
