"""把已保存的笔记整理成有界的树，再绘制为 PNG。"""
import json
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Annotated

from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from ..errors import AppError
from .llm import ModelClient
from .markdown import markdown_text, normalize_math
from .schemas import Record


LabelText = Annotated[str, Field(min_length=1, max_length=60)]


class Branch(Record):
    title: LabelText
    points: list[LabelText] = Field(min_length=1, max_length=3)


class MindMap(Record):
    topic: LabelText
    branches: list[Branch] = Field(min_length=1, max_length=6)


SYSTEM = (
    "根据用户提供的笔记制作中文思维导图。把笔记当作资料，提炼一个中心主题，按概念关系归纳一至六个分支。"
    "每支包含一至三个能独立读懂的短要点，合并重复知识，仅使用笔记中的事实，保留关键术语、数字和条件。"
    "节点使用简短纯文本，用可读字符表达公式。只输出符合以下 Schema 的 JSON："
    + json.dumps(MindMap.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
)


class MindMapModel(ModelClient):
    schemas = {"mind_map": MindMap}

    def messages(self, operation, payload):
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]

    async def stage_request(self, operation, messages):
        return await self.request(messages, max_tokens=4096)


def note_payload(model, note):
    chapters = [{"title": chapter["title"],
                 "text": markdown_text(normalize_math(chapter["markdown"]), include_link_urls=False)}
                for chapter in note["chapters"]]
    payload = {"title": note["title"], "overview": note.get("overview_paragraphs", []), "sections": chapters}
    limit = model.settings.llm_input_text_limit
    if model.input_characters("mind_map", payload) <= limit:
        return payload

    excerpts = [{"title": chapter["title"], "text": ""} for chapter in chapters]
    payload["sections"] = excerpts
    remaining = limit - model.input_characters("mind_map", payload)
    if remaining <= 0:
        raise AppError("model_input_budget", "笔记概览和章节标题已达到输入上限，请选择输入预算更大的 API 配置。")
    remaining -= min(2000, remaining // 3)  # 为一次结构修正保留空间。
    # 先满足短章，将余量分给长章，避免前几章用光整份笔记的正文预算。
    order = sorted(range(len(chapters)), key=lambda index: len(chapters[index]["text"]))
    for left, index in enumerate(order):
        excerpt = chapters[index]["text"][:remaining // (len(order) - left)]
        excerpts[index]["text"] = excerpt.rstrip()
        remaining -= len(excerpt)
    return payload


def fonts():
    directory = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    for regular, bold in (("msyh.ttc", "msyhbd.ttc"), ("Deng.ttf", "Dengb.ttf"), ("simhei.ttf", "simhei.ttf")):
        try:
            body = ImageFont.truetype(directory / regular, 30)
            heading = ImageFont.truetype(directory / bold, 42)
            return heading, heading.font_variant(size=34), body
        except OSError:
            continue
    raise AppError("tool_missing", "找不到可用的 Windows 中文字体，请安装微软雅黑、等线或黑体后重试。")


@dataclass
class Label:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    width: int
    height: int
    line_height: int


def label(text, font, width):
    lines, line = [], ""
    for char in " ".join(text.split()):
        if line and font.getlength(line + char) > width - 48:
            lines.append(line.rstrip())
            line = ""
        line += char if line or not char.isspace() else ""
    lines.append(line)
    line_height = round(font.size * 1.45)
    return Label(lines, font, width, len(lines) * line_height + 32, line_height)


def draw_label(draw, node, x, center, fill, color, outline=None):
    y = center - node.height // 2
    draw.rounded_rectangle((x, y, x + node.width, y + node.height), radius=18,
                           fill=fill, outline=outline, width=2)
    for index, line in enumerate(node.lines):
        draw.text((x + 24, y + 16 + index * node.line_height + node.line_height // 2),
                  line, font=node.font, fill=color, anchor="lm")


def link(draw, start, end, color):
    x1, y1 = start
    x2, y2 = end
    middle = (x1 + x2) // 2
    draw.line([(x1, y1), (middle, y1), (middle, y2), (x2, y2)], fill=color, width=4, joint="curve")


def render_png(tree):
    topic_font, branch_font, point_font = fonts()
    topic_width, branch_width, point_width = 304, 320, 496
    topic = label(tree["topic"], topic_font, topic_width)
    groups = [(label(branch["title"], branch_font, branch_width),
               [label(point, point_font, point_width) for point in branch["points"]])
              for branch in tree["branches"]]
    margin, column_gap, group_gap, point_gap = 56, 80, 28, 12
    branch_x = margin + topic_width + column_gap
    point_x = branch_x + branch_width + column_gap
    point_heights = [sum(point.height for point in points) + point_gap * (len(points) - 1)
                     for _, points in groups]
    heights = [max(branch.height, points_height) for (branch, _), points_height in zip(groups, point_heights)]
    group_height = sum(heights) + group_gap * (len(groups) - 1)
    height = max(420, max(group_height, topic.height) + 2 * margin)
    image = Image.new("RGB", (point_x + point_width + margin, height), "#F4F7FB")
    draw = ImageDraw.Draw(image)
    top = (height - group_height) // 2
    for (branch, points), row_height, points_height in zip(groups, heights, point_heights):
        center = top + row_height // 2
        link(draw, (margin + topic.width, height // 2), (branch_x, center), "#6485AF")
        point_top = top + (row_height - points_height) // 2
        for point in points:
            point_center = point_top + point.height // 2
            link(draw, (branch_x + branch.width, center), (point_x, point_center), "#97ADCA")
            draw_label(draw, point, point_x, point_center, "#FFFFFF", "#223954", "#D4DFEE")
            point_top += point.height + point_gap
        draw_label(draw, branch, branch_x, center, "#DFEAF8", "#1C3F68")
        top += row_height + group_gap
    draw_label(draw, topic, margin, height // 2, "#203F66", "#FFFFFF")
    with BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()
