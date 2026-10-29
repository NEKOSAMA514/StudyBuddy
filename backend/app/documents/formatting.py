"""文档来源标签、纯文本转义与表格格式化。"""
import re
from ..analysis.markdown import parser, map_links

SOURCE_LINK = r"(?<!\\)\[[^\]\n]*\]\(#source-(?P<source>D[1-9]\d*)(?:-(?P<end>D[1-9]\d*))?\)"
DOCUMENT_LINK = r"(?<!\\)\[(?P<label>(?:\\.|[^\]\\\n])*)\]\(#document-unit-D[1-9]\d*\)"


def render_sources(markdown, sources):
    expected, heading = 0, False
    order = {identifier: index for index, identifier in enumerate(sources)}
    for token in parser.parse(markdown):
        if token.type in {"heading_open", "heading_close"}:
            heading = token.type == "heading_open"
        links = []
        for child in token.children or []:
            if child.type != "link_open":
                continue
            href = child.attrGet("href") or ""
            if href.startswith("#document-unit-"):
                raise ValueError("document_source_link_must_use_primary_id")
            if href.startswith("#source-"):
                match = re.fullmatch(r"#source-(D[1-9]\d*)(?:-(D[1-9]\d*))?", href)
                if not match or any(identifier not in sources for identifier in match.groups() if identifier):
                    raise ValueError("document_source_must_belong_to_primary")
                if not heading:
                    raise ValueError("document_source_link_only_in_section_heading")
                start, end = match.groups()
                if end and order[end] < order[start]:
                    raise ValueError("document_source_range_must_follow_original_order")
                links.append(start)
        if heading and token.type == "inline":
            if len(links) != 1:
                raise ValueError("each_section_heading_requires_one_[来源](#source-D1)_or_range_link")
            expected += len(links)
    if not expected:
        raise ValueError("section_heading_source_link_required_use_[来源](#source-D1)")
    replaced = 0
    def replace(match):
        nonlocal replaced
        identifier = match.group("source")
        replaced += 1
        label = sources[identifier]
        end = match.group("end")
        if end and sources[end] != label:
            label += "–" + sources[end]
        label = literal("来源：" + label).replace("\n", " ").replace("\r", " ")
        return f"[{label}](#document-unit-{identifier})"
    result = map_links(markdown, SOURCE_LINK, replace)
    if replaced != expected:
        raise ValueError("document_source_link_format_must_be_[来源](#source-D1)")
    return result


def plain_sources(markdown):
    return map_links(markdown, DOCUMENT_LINK, lambda match: match.group("label"))


def literal(value):
    return re.sub(r"([\\`*_{}\[\]<>()#$!|~])", r"\\\1", str(value if value is not None else ""))


def cell(value):
    return literal(value).replace("\n", " / ").replace("\r", "")


def table(rows):
    width = max((len(row) for row in rows), default=0)
    if not width:
        return ""
    lines = ["| " + " | ".join(cell(value) for value in list(row) + [""] * (width - len(row))) + " |" for row in rows]
    # 使用生成列名，让原始第一行继续作为数据保留。
    return "\n".join(["| " + " | ".join(str(i + 1) for i in range(width)) + " |",
                      "| " + " | ".join(["---"] * width) + " |", *lines])
