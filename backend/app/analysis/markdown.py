"""解析共用 Markdown 正文，用于校验和可读纯文本导出。"""
import re
import json
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def map_links(markdown, pattern, replace):
    """替换匹配链接，同时逐字保留行内代码和代码围栏。"""
    inline = re.compile(r"(?P<code>(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`))|" + pattern)
    def plain(text):
        return inline.sub(lambda match: match.group() if match.group("code") else replace(match), text)
    lines, parts, cursor = markdown.splitlines(keepends=True), [], 0
    for token in parser.parse(markdown):
        if token.type in {"fence", "code_block"}:
            start, end = token.map
            parts.extend([plain("".join(lines[cursor:start])), "".join(lines[start:end])])
            cursor = end
    parts.append(plain("".join(lines[cursor:])))
    return "".join(parts)


# TXT 保留公式源码，防止普通 Markdown 转义吃掉矩阵换行、花括号等符号。
text_parser = MarkdownIt("commonmark").enable(["table", "strikethrough"]).use(
    dollarmath_plugin, allow_labels=False, double_inline=True)

math_parts = re.compile(
    r"(?P<code>(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<math>(?<![\\$])(?P<dollars>\${1,2})(?!\$)[\s\S]+?(?<!\\)(?P=dollars)(?!\$))"
    r"|(?<!\\)\\\[(?P<display>[\s\S]+?)\\\]"
    r"|(?<!\\)\\\((?P<inline>[\s\S]+?)\\\)"
    r"|(?P<chem>(?<!\\)\\ce\{)")


# 只规范正文中的公式定界与化学表达式，代码区域保持原文。
def normalize_math(markdown):
    """规范公式定界符，仅构造显示/导出副本，保留代码及化学式内容。"""
    def display(value, indent):
        # 列表内的公式正文和结束定界符必须保留同一层缩进。
        lines = value.strip().splitlines()
        body = "\n".join(indent + (line.removeprefix(indent) if i else line) for i, line in enumerate(lines))
        return "$$\n" + body + "\n" + indent + "$$"

    def plain(text):
        parts, cursor = [], 0
        while match := math_parts.search(text, cursor):
            start, end = match.span()
            parts.append(text[cursor:start])
            value = match.group()
            if match.group("chem"):
                depth = 1
                while end < len(text) and depth:
                    if text[end] == "\\":
                        end += 2
                        continue
                    depth += (text[end] == "{") - (text[end] == "}")
                    end += 1
                value = text[start:end]
                if depth:
                    parts.append(value)
                    cursor = end
                    continue
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            standalone = not text[line_start:start].strip() and not text[end:line_end if line_end >= 0 else len(text)].strip()
            indent = text[line_start:start] if standalone else ""
            if match.group("display") is not None:
                value = display(match.group("display"), indent) if indent else "\n" + display(match.group("display"), "") + "\n"
            elif match.group("inline") is not None:
                value = "$" + match.group("inline").strip() + "$"
            elif match.group("chem"):
                value = display(value, indent) if standalone else "$" + value + "$"
            elif match.group("math") and match.group("dollars") == "$$" and standalone:
                value = display(value[2:-2], indent)
            parts.append(value)
            cursor = end
        return "".join(parts) + text[cursor:]

    lines, parts, cursor = markdown.splitlines(keepends=True), [], 0
    for token in parser.parse(markdown):
        if token.type in {"fence", "code_block", "html_block"}:
            start, end = token.map
            parts.extend([plain("".join(lines[cursor:start])), "".join(lines[start:end])])
            cursor = end
    parts.append(plain("".join(lines[cursor:])))
    return "".join(parts)


def validate_markdown(markdown):
    if not has_markdown_body(markdown):
        raise ValueError("chapter_needs_body_content")
    for token in parser.parse(markdown):
        if token.type == "html_block" and re.search(r"<!--\s*(chapter|summary|evidence|issue)\s*:", token.content):
            raise ValueError("internal_marker_in_body")
        if token.type == "inline":
            for child in token.children or []:
                if child.type == "html_inline" and re.search(r"<!--\s*(chapter|summary|evidence|issue)\s*:", child.content):
                    raise ValueError("internal_marker_in_body")
                if child.type == "text" and re.search(r"(?:\[(?:E\d+)(?:\s*[,，]\s*E\d+)*\]|(?:证据|evidence)\s*[:：]?\s*E\d+)", child.content, re.I):
                    raise ValueError("evidence_reference_in_body")


def has_markdown_body(markdown):
    heading = False
    for token in parser.parse(markdown):
        if token.type in {"heading_open", "heading_close"}:
            heading = token.type == "heading_open"
        elif token.type in {"fence", "code_block"} and token.content.strip():
            return True
        elif token.type == "inline" and not heading and inline_text(token.children).strip():
            return True
    return False


def inline_text(children, *, include_link_urls=True):
    parts, links = [], []
    for token in children or []:
        if token.type in {"text", "code_inline"}:
            parts.append(token.content)
        elif token.type in {"math_inline", "math_inline_double"}:
            parts.append(token.markup + token.content + token.markup)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.type == "link_open":
            links.append((token.attrGet("href"), len(parts)))
        elif token.type == "link_close":
            href, start = links.pop()
            if include_link_urls and href and "".join(parts[start:]) != href:
                parts.append(f" ({href})")
    return "".join(parts)


def markdown_text(markdown, *, include_link_urls=True):
    lines, lists, cells, prefix = [], [], None, ""
    for token in text_parser.parse(markdown):
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            lists.append(None if token.type == "bullet_list_open" else int(token.attrGet("start") or 1))
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif token.type == "list_item_open":
            prefix = "  " * (len(lists) - 1) + ("- " if lists[-1] is None else f"{lists[-1]}. ")
            if lists[-1] is not None:
                lists[-1] += 1
        elif token.type == "tr_open":
            cells = []
        elif token.type == "tr_close":
            lines.append(" | ".join(cells))
            cells = None
        elif token.type == "inline":
            text = inline_text(token.children, include_link_urls=include_link_urls)
            if cells is not None:
                cells.append(text)
            else:
                lines.append(prefix + text)
                prefix = ""
        elif token.type in {"fence", "code_block"}:
            lines.append(token.content.rstrip("\n"))
            lines.append("")
            prefix = ""
        elif token.type == "math_block":
            lines.extend([prefix + token.markup + token.content + token.markup, ""])
            prefix = ""
        elif token.type in {"paragraph_close", "heading_close", "table_close", "blockquote_close"}:
            if lines and lines[-1] and not lists:
                lines.append("")
    return "\n".join(lines).strip("\n")


# 解析章节与摘要协议标记，将唯一 Markdown 正文交给后续结构校验。
def parse_chapters(content):
    lines = content.splitlines(keepends=True)
    chapters, current, cursor = [], None, 0

    def body(end):
        text = "".join(lines[cursor:end]).strip()
        if current is not None:
            current["markdown"] += ("\n\n" if current["markdown"] and text else "") + text
        elif text:
            raise ValueError("markdown_requires_chapter_marker")

    for token in parser.parse(content):
        if token.type != "html_block" or token.level != 0:
            continue
        marker = re.fullmatch(r"<!--\s*(chapter|summary|issue)\s*:(.*?)\s*-->",
                              token.content.strip(), re.S)
        if marker is None:
            continue
        body(token.map[0])
        kind, value = marker.group(1), marker.group(2).strip()
        if kind == "chapter":
            current = {"chapter_id": value, "summary": "", "markdown": "", "issues": []}
            chapters.append(current)
        elif current is None:
            raise ValueError("markdown_requires_chapter_marker")
        elif kind == "summary":
            if current["summary"] or current["markdown"]:
                current["protocol_error"] = "markdown_summary_must_precede_body_once"
            else:
                current["summary"] = value
        else:
            try:
                issue = json.loads(value)
                if not isinstance(issue, dict):
                    raise ValueError("markdown_issue_requires_object")
                current["issues"].append({"kind": "source_issue", **issue})
            except ValueError:
                current["issues"].append({"invalid_marker": value})
        cursor = token.map[1]
    body(len(lines))
    return {"chapters": chapters}
