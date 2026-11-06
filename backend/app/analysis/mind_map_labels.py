"""测量和绘制导图标签，支持公式上下标与数学字体回退。"""
import os
import re
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path

from PIL import ImageFont

from ..errors import AppError


FORMULA = re.compile(r"\\\((.*?)\\\)", re.S)
SCRIPT = re.compile(r"([_^])\{([^{}\\_^]{1,8})\}")
ATOM = re.compile(rf"(.)( (?:{SCRIPT.pattern})* )", re.X)
WORDS = re.compile(r"[A-Za-z0-9]+|.")


def label_text(value):
    def visible(match):
        formula = " ".join(match[1].split())
        parts = []
        for atom in ATOM.finditer(formula):
            base, scripts = atom[1], SCRIPT.findall(atom[2])
            if base in r"\{}_^" or (base.isspace() and scripts) or len(dict(scripts)) != len(scripts):
                raise ValueError("公式内使用普通字符，每个基字符可带一个 _{下标} 和一个 ^{上标}。")
            parts.append(base + "".join(text for _, text in scripts))
        if not parts:
            raise ValueError("公式需要正文。")
        return "".join(parts)

    if len(FORMULA.sub(visible, value)) > 60:
        raise ValueError("节点可见文字最多 60 字。")
    return value


def glyph_mask(font, char):
    mask = font.getmask(char, mode="L")
    return mask.size, bytes(mask)


class FontStack:
    def __init__(self, primary, alternatives):
        self.primary = primary
        self.faces = [primary, *(face.font_variant(size=primary.size) for face in alternatives)]
        self.missing = [glyph_mask(face, "\uffff") for face in self.faces]
        self.cache = {}

    def pick(self, char):
        if char not in self.cache:
            self.cache[char] = next((face for face, missing in zip(self.faces, self.missing)
                                     if glyph_mask(face, char) != missing), self.primary)
        return self.cache[char]


def fonts():
    directory = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    for regular, bold in (("msyh.ttc", "msyhbd.ttc"), ("Deng.ttf", "Dengb.ttf"), ("simhei.ttf", "simhei.ttf")):
        try:
            body = ImageFont.truetype(directory / regular, 30)
            heading = ImageFont.truetype(directory / bold, 42)
            break
        except OSError:
            continue
    else:
        raise AppError("tool_missing", "找不到可用的 Windows 中文字体，请安装微软雅黑、等线或黑体后重试。")
    symbols = []
    for name, index in (("cambria.ttc", 1), ("seguisym.ttf", 0)):
        try:
            symbols.append(ImageFont.truetype(directory / name, 30, index=index))
        except OSError:
            continue
    return [(FontStack(font, symbols), FontStack(font.font_variant(size=round(font.size * 0.65)), symbols))
            for font in (heading, heading.font_variant(size=34), body)]


@dataclass
class Glyph:
    text: str
    font: ImageFont.FreeTypeFont
    x: float
    y: int = 0


@dataclass
class Label:
    glyphs: list[Glyph]
    width: int
    height: int


def runs(text, face, x=0.0, y=0):
    start = x
    glyphs = []
    for font, chars in groupby(text, face.pick):
        value = "".join(chars)
        glyphs.append(Glyph(value, font, x, y))
        x += font.getlength(value)
    return glyphs, x - start


def formula_atoms(expression, face, script_face):
    for atom in ATOM.finditer(expression):
        glyphs, base_width = runs(atom[1], face)
        widths = [0.0]
        for position, text in SCRIPT.findall(atom[2]):
            shift = round(face.primary.size * (0.22 if position == "_" else -0.44))
            placed, advance = runs(text, script_face, base_width, shift)
            glyphs.extend(placed)
            widths.append(advance)
        yield glyphs, base_width + max(widths)


def words(text, face, limit):
    for token in WORDS.findall(text):
        glyphs, advance = runs(token, face)
        if advance > limit:
            yield from (runs(char, face) for char in token)
        else:
            yield glyphs, advance


def label(text, style, width):
    face, script_face = style
    limit = width - 48
    rows, used = [[]], 0.0
    for index, segment in enumerate(FORMULA.split(" ".join(text.split()))):
        atoms = list(formula_atoms(segment, face, script_face)) if index % 2 else words(segment, face, limit)
        if index % 2:
            total = sum(advance for _, advance in atoms)
            # 短公式整段换行，长公式按含有上下标的字符组换行。
            if total <= limit < used + total:
                rows.append([])
                used = 0.0
        for glyphs, advance in atoms:
            if rows[-1] and used + advance > limit:
                rows.append([])
                used = 0.0
            if not rows[-1] and glyphs[0].text.isspace():
                continue
            rows[-1].extend(replace(glyph, x=glyph.x + used) for glyph in glyphs)
            used += advance

    top, placed = 16, []
    for row in filter(None, rows):
        bounds = [(glyph.font.getbbox(glyph.text, anchor="ls"), glyph.y) for glyph in row]
        above = min(box[1] + offset for box, offset in bounds)
        below = max(box[3] + offset for box, offset in bounds)
        height = max(round(face.primary.size * 1.45), below - above + 6)
        baseline = top + (height - (below - above)) // 2 - above
        placed.extend(replace(glyph, y=glyph.y + baseline) for glyph in row)
        top += height
    return Label(placed, width, top + 16)


def draw_label(draw, node, x, center, fill, color, outline=None):
    top = center - node.height // 2
    draw.rounded_rectangle((x, top, x + node.width, top + node.height), radius=18,
                           fill=fill, outline=outline, width=2)
    for glyph in node.glyphs:
        draw.text((x + 24 + glyph.x, top + glyph.y), glyph.text, font=glyph.font, fill=color, anchor="ls")
