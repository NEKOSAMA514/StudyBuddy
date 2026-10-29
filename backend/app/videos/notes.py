"""TXT与Markdown下载读取同一份章节Markdown，不重新生成正文。"""
import re
from sqlalchemy import select
from ..db import Task
from .schemas import current_notes
from ..util.time import timestamp
from ..analysis.markdown import markdown_text
from .timestamps import plain_timestamps
from ..note_marks import clear_marks


def partial_notice(result):
    if result.get("complete", True):
        return []
    if result.get("note_style") == "concise":
        cursor = result["source_cursor"]
        return [f"未完成笔记：已完成 {result['completed_segments']}/{result['total_segments']} 段，"
                f"处理至原记录 {cursor['evidence_id']} 的第 {cursor['offset']} 字符。", ""]
    return [f"未完成笔记：已完成 {result['completed_segments']}/{result['total_segments']} 段，"
            f"覆盖 00:00–{timestamp(result['covered_until_seconds'])}。", ""]


def notes_text(filename: str, duration: float, result: dict) -> str:
    if result.get("note_style") == "concise":
        return markdown_text(concise_markdown(filename, duration, result))
    title = " ".join(result["title"].splitlines())
    lines = [*partial_notice(result), "视频笔记", "=" * 40, f"标题：{title}", f"来源视频：{filename}",
             f"视频时长：{timestamp(duration)}", "", "一、整体概览", "-" * 40]
    for paragraph in result["overview_paragraphs"]:
        lines.extend([paragraph, ""])
    lines.extend(["二、关键标签", "-" * 40,
                  "、".join(result["tags"]) or "暂无标签。", "", "三、正文", "-" * 40])
    guides = {guide["first_chapter_id"]: guide for guide in result.get("segment_guides", [])}
    for chapter in result["chapters"]:
        if guide := guides.get(chapter["chapter_id"]):
            lines.extend([f"第{guide['index']}段导览 [{timestamp(guide['start_seconds'])}–{timestamp(guide['end_seconds'])}]",
                          "-" * 40, guide["summary"], ""])
        lines.extend([chapter["title"], "-" * 40,
                      f"[{timestamp(chapter['start_seconds'])}–{timestamp(chapter['end_seconds'])}]",
                      markdown_text(chapter["markdown"]), ""])
    if result["source_issues"]:
        lines.extend(["存疑部分：", "-" * 40])
        for issue in result["source_issues"]:
            if issue.get("quote"):
                lines.append(f"原文：{issue['quote']}")
            lines.extend([f"存疑原因：{issue['reason']}", ""])
    return "\n".join(lines).rstrip() + "\n"


def escape_markdown(text):
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", text)


# 从已保存正文构造 Markdown 导出，部分结果附带未完成说明。
def notes_markdown(filename: str, duration: float, result: dict) -> str:
    if result.get("note_style") == "concise":
        return concise_markdown(filename, duration, result)
    lines = [*partial_notice(result), f"# {escape_markdown(' '.join(result['title'].splitlines()))}", "", f"来源视频：{escape_markdown(' '.join(filename.splitlines()))}", "",
             f"视频时长：{timestamp(duration)}", "", "## 整体概览", ""]
    for paragraph in result["overview_paragraphs"]:
        lines.extend([escape_markdown(paragraph), ""])
    lines.extend(["## 关键标签", "", "、".join(escape_markdown(tag) for tag in result["tags"]) or "暂无标签。",
                  "", "## 正文", ""])
    guides = {guide["first_chapter_id"]: guide for guide in result.get("segment_guides", [])}
    for chapter in result["chapters"]:
        if guide := guides.get(chapter["chapter_id"]):
            lines.extend([f"### 第{guide['index']}段导览 [{timestamp(guide['start_seconds'])}–{timestamp(guide['end_seconds'])}]",
                          "", escape_markdown(guide["summary"]), ""])
        lines.extend([f"### {escape_markdown(chapter['title'])}", "",
                      f"[{timestamp(chapter['start_seconds'])}–{timestamp(chapter['end_seconds'])}]", "",
                      chapter["markdown"], ""])
    if result["source_issues"]:
        lines.extend(["## 存疑部分", ""])
        for issue in result["source_issues"]:
            if issue.get("quote"):
                lines.extend([f"原文：{escape_markdown(issue['quote'])}", ""])
            lines.extend([f"存疑原因：{escape_markdown(issue['reason'])}", ""])
    return "\n".join(lines).rstrip() + "\n"


def concise_markdown(filename, duration, result):
    lines = [*partial_notice(result), f"# {escape_markdown(result['title'])}", "",
             f"来源视频：{escape_markdown(filename)}", "", f"视频时长：{timestamp(duration)}", ""]
    for chapter in result["chapters"]:
        if result["total_segments"] > 1:
            lines.extend([f"## {escape_markdown(chapter['title'])}", ""])
        lines.extend([plain_timestamps(chapter["markdown"]), "", f"来源定位：{timestamp(chapter['start_seconds'])}–{timestamp(chapter['end_seconds'])}", ""])
    return "\n".join(lines).rstrip() + "\n"


def completed_notes(session, video_id):
    tasks = session.scalars(select(Task).where(Task.video_id == video_id, Task.status == "succeeded")
                            .order_by(Task.updated_at.desc(), Task.id.desc()))
    return [task for task in tasks if current_notes(task.result) and task.result.get("complete", True)]


# 成功事务内保留两份完整笔记，旧版本正文和关联重点同步淘汰。
def retain_two_notes(session, video_id):
    for task in completed_notes(session, video_id)[2:]:
        clear_marks(session, "video", [task.id])
        task.result = None
        task.config = {key: value for key, value in task.config.items() if key != "outline_preview"}
