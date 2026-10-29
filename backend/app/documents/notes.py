"""文档笔记读取、导出及事务内的成功版本保留。"""
from pathlib import Path
from sqlalchemy import select
from ..db import DocumentTask
from ..errors import AppError
from ..analysis.markdown import normalize_math, markdown_text
from .schemas import public_task
from .task_rules import get_task
from ..note_marks import clear_marks
from .formatting import plain_sources


def completed(session, identifier):
    tasks = session.scalars(select(DocumentTask).where(DocumentTask.document_id == identifier, DocumentTask.status == "succeeded")
                            .order_by(DocumentTask.completed_at.desc(), DocumentTask.id.desc()))
    return [t for t in tasks if t.result and t.result.get("complete") and t.result.get("chapters")]


# 在成功提交事务内只保留最近两份完整笔记，同步移除被淘汰版本的重点。
def retain_two(session, identifier):
    for task in completed(session, identifier)[2:]:
        clear_marks(session, "document", [task.id])
        task.result = None
        task.config = {k: v for k, v in task.config.items() if k != "outline_preview"}


# 读取和导出当前或指定任务的文档笔记，不触发重新生成。
class DocumentNotes:
    def __init__(self, documents, sessions):
        self.documents, self.sessions = documents, sessions

    def versions(self, identifier):
        with self.sessions() as session:
            self.documents.get(session, identifier)
            return [public_task(t) for t in completed(session, identifier)[:2]]

    def analysis(self, identifier):
        with self.sessions() as session:
            task = get_task(session, identifier)
            if not task.result or not task.result.get("chapters"):
                raise AppError("notes_unavailable", "暂无可用笔记。", 409)
            result = {**task.result, "id": task.id, "document_id": task.document_id, "notes_revision": task.notes_revision,
                      "warnings": task.warnings, "timings": task.timings, "request_count": task.request_count}
            if result.get("note_style") == "concise":
                from .concise import validate_result
                from .material import load_material, verify_snapshot
                material = load_material(self.documents.folder(task.document_id))
                verify_snapshot(material, task.config["source"])
                validate_result(result, material)
            result["chapters"] = [{**c, "markdown": normalize_math(c["markdown"])} for c in result["chapters"]]
            return result

    def download(self, identifier, format):
        result = self.analysis(identifier)
        with self.sessions() as session:
            document = self.documents.get(session, result["document_id"])
            filename = f"{Path(document.filename).stem} 笔记总结.{format}"
        lines = [f"# {result['title']}"]
        if not result["complete"]:
            lines.append(f"未完成笔记：已完成 {result['completed_segments']}/{result['total_segments']} 段。")
        lines.extend(result["overview_paragraphs"])
        for chapter in result["chapters"]:
            if result.get("note_style") != "concise" or result["total_segments"] > 1:
                lines.append(f"## {chapter['title']}")
            markdown = plain_sources(chapter["markdown"]) if result.get("note_style") == "concise" else chapter["markdown"]
            lines.extend([markdown, "来源：" + "；".join(chapter["source_labels"])])
        if result["source_issues"]:
            lines.append("## 存疑部分")
            lines.extend(i["reason"] + (" 原文：" + i["quote"] if i["quote"] else "") for i in result["source_issues"])
        lines.extend(result.get("warnings", []))
        text = "\n\n".join(lines)
        return text if format == "md" else markdown_text(text), filename
