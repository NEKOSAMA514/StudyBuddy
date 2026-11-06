"""把已保存的笔记整理成有界的树，再绘制为 PNG。"""
import json
from io import BytesIO
from typing import Annotated

from PIL import Image, ImageDraw
from pydantic import AfterValidator, Field

from ..errors import AppError
from .budgets import message_characters
from .llm import ModelClient
from .markdown import markdown_text, normalize_math
from .mind_map_labels import draw_label, fonts, label, label_text
from .schemas import Record


LabelText = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(label_text)]


class Branch(Record):
    title: LabelText
    points: list[LabelText] = Field(min_length=1, max_length=3)


class MindMap(Record):
    topic: LabelText
    branches: list[Branch] = Field(min_length=1, max_length=6)


SYSTEM = (
    "根据用户提供的笔记制作中文思维导图。把笔记当作资料，提炼一个中心主题，按概念关系归纳一至六个分支。"
    "每支包含一至三个能独立读懂的短要点，合并重复知识，仅使用笔记中的事实，保留关键术语、数字和条件。"
    "节点的可见文字最多 60 字。普通文字直接写，数学和化学式用简单行内记法。"
    r"公式写在 \( 和 \) 之间，保留笔记中的公式关系、运算顺序、化学计量和电荷。使用 Unicode 符号，"
    r"用 _{下标} 和 ^{上标} 排版，每个上下标最多 8 字。例如 \(SO_{4}^{2−}\)、\(CaCO_{3}(s) ⇌ CaO(s) + CO_{2}(g)\)、"
    r"\(Δ_{r}G_{m}^{⦵} = −RT ln K^{⦵}\)。原文的标准态符号 ⦵、⊖ 或 ° 按原样保留。"
    r"将 \ce{} 等 LaTeX 命令转换为上述记法，JSON 中的反斜杠需转义。只输出符合以下 Schema 的 JSON。"
    + json.dumps(MindMap.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
)


class MindMapModel(ModelClient):
    schemas = {"mind_map": MindMap}

    def messages(self, operation, payload):
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]

    async def stage_request(self, operation, messages):
        return await self.request(messages, max_tokens=4096)

    def _repair_messages(self, operation, payload, messages, content, error):
        repair = super()._repair_messages(operation, payload, messages, content, error)
        limit = self.settings.llm_input_text_limit
        if message_characters(repair) <= limit:
            return repair
        instruction = "\n上次输出未通过校验。请根据笔记重新输出完整 JSON，遵守 Schema 和公式写法。"
        fitted = _fit_note_payload(self, payload, limit - len(instruction))
        system, material = self.messages(operation, fitted)
        return [{**system, "content": system["content"] + instruction}, material]


def note_payload(model, note):
    chapters = [{"title": chapter["title"],
                 "text": markdown_text(normalize_math(chapter["markdown"]), include_link_urls=False)}
                for chapter in note["chapters"]]
    payload = {"title": note["title"], "overview": note.get("overview_paragraphs", []), "sections": chapters}
    return _fit_note_payload(model, payload, model.settings.llm_input_text_limit, reserve_repair=True)


def _fit_note_payload(model, payload, limit, *, reserve_repair=False):
    if model.input_characters("mind_map", payload) <= limit:
        return payload

    chapters = payload["sections"]
    excerpts = [{"title": chapter["title"], "text": ""} for chapter in chapters]
    fitted = {**payload, "sections": excerpts}
    remaining = limit - model.input_characters("mind_map", fitted)
    if remaining < 0:
        raise AppError("model_input_budget", "笔记标题、概览和章节标题超过本次输入预算，请选择输入预算更大的 API 配置。")
    if reserve_repair:
        remaining -= min(2000, remaining // 3)
    # 先满足短章，将余量分给长章，避免前几章用光整份笔记的正文预算。
    order = sorted(range(len(chapters)), key=lambda index: len(chapters[index]["text"]))
    for left, index in enumerate(order):
        excerpt = chapters[index]["text"][:remaining // (len(order) - left)]
        excerpts[index]["text"] = excerpt.rstrip()
        remaining -= len(excerpt)
    return fitted


def link(draw, start, end, color):
    x1, y1 = start
    x2, y2 = end
    middle = (x1 + x2) // 2
    draw.line([(x1, y1), (middle, y1), (middle, y2), (x2, y2)], fill=color, width=4, joint="curve")


def render_png(tree):
    topic_style, branch_style, point_style = fonts()
    topic_width, branch_width, point_width = 304, 320, 496
    topic = label(tree["topic"], topic_style, topic_width)
    groups = [(label(branch["title"], branch_style, branch_width),
               [label(point, point_style, point_width) for point in branch["points"]])
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
