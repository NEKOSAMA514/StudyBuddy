"""PDFium 读取字形，pypdf 校验、读取绘制资源及写出批次；原生句柄只留在本模块。"""
import ctypes
import hashlib
import io
import math
import threading
from contextlib import contextmanager, closing

from ..errors import AppError, TaskInterrupted

PDFIUM_LOCK = threading.RLock()
MAX_CONTENT_BYTES = 16 * 1024 * 1024


@contextmanager
def pdfium_lock(check=lambda: None):
    check()
    while not PDFIUM_LOCK.acquire(timeout=0.05):
        check()
    try:
        check()
        yield
    finally:
        PDFIUM_LOCK.release()


def check_size(width, height):
    from .image_text import MAX_IMAGE_PIXELS
    if width * height > MAX_IMAGE_PIXELS:
        raise AppError("document_limit", "文档单张图片超过2000万像素，请缩小图片后上传。")
    if width <= 0 or height <= 0:
        raise ValueError("invalid PDF image dimensions")


def content_data(stream):
    data = stream.get_data() if stream is not None else b""
    if len(data) > MAX_CONTENT_BYTES:
        raise AppError("document_limit", "PDF 单页内容过大，请拆分或压缩后上传。")
    return data


def drawn_images(page, reader, check):
    """只遍历执行的 Do 和 BI；Form 的资源、变换及绘制顺序随调用递归。"""
    from pypdf._utils import matrix_multiply
    from pypdf.generic import ContentStream, DecodedStreamObject, EncodedStreamObject, NameObject, ArrayObject
    from pypdf._page import _INLINE_IMAGE_KEY_MAPPING, _INLINE_IMAGE_VALUE_MAPPING

    def matrix(values):
        a, b, c, d, e, f = map(float, values)
        return ((a, b, 0), (c, d, 0), (e, f, 1))

    identity = matrix((1, 0, 0, 1, 0, 0))
    candidates, active = [], set()
    consumed = 0

    def walk(stream, resources, transform):
        nonlocal consumed
        check()
        data = content_data(stream)
        consumed += len(data)
        if consumed > MAX_CONTENT_BYTES:
            raise AppError("document_limit", "PDF 单页内容过大，请拆分或压缩后上传。")
        stack = []
        decoded = DecodedStreamObject()
        decoded.set_data(data)
        for operands, operator in ContentStream(decoded, reader).operations:
            check()
            if operator == b"q":
                stack.append(transform)
            elif operator == b"Q":
                if stack:
                    transform = stack.pop()
            elif operator == b"cm":
                transform = matrix_multiply(matrix(operands), transform)
            elif operator in {b"Do", b"INLINE IMAGE"}:
                if operator == b"INLINE IMAGE":
                    image = EncodedStreamObject()
                    image._data = operands["data"]
                    for key, value in operands["settings"].items():
                        key = NameObject(_INLINE_IMAGE_KEY_MAPPING.get(key, key))
                        def expand(value):
                            if isinstance(value, NameObject):
                                value = NameObject(_INLINE_IMAGE_VALUE_MAPPING.get(value, value))
                                if key == "/ColorSpace":
                                    value = resources.get("/ColorSpace", {}).get(value, value)
                            return value
                        image[key] = ArrayObject([expand(v) for v in value]) if isinstance(value, list) else expand(value)
                else:
                    image = resources.get("/XObject", {})[operands[0]].get_object()
                    if image.get("/Subtype") == "/Form":
                        marker = id(image)
                        if marker in active:
                            raise ValueError("cyclic PDF Form")
                        active.add(marker)
                        try:
                            walk(image, image.get("/Resources", resources),
                                 matrix_multiply(matrix(image.get("/Matrix", (1, 0, 0, 1, 0, 0))), transform))
                        finally:
                            active.remove(marker)
                        continue
                    if image.get("/Subtype") != "/Image":
                        continue
                points = [(x * transform[0][0] + y * transform[1][0] + transform[2][0],
                           x * transform[0][1] + y * transform[1][1] + transform[2][1])
                          for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
                candidates.append((image, (min(x for x, y in points), min(y for x, y in points),
                                           max(x for x, y in points), max(y for x, y in points))))
    walk(page.get_contents(), page.get("/Resources", {}), identity)
    return candidates


def image_identity(image):
    # Inline 图片没有间接引用；编码流连同字典（含蒙版引用）确定其身份，不解码像素。
    reference = getattr(image, "indirect_reference", None)
    if reference is not None:
        return (reference.idnum, reference.generation)
    return hashlib.sha256(repr(sorted(image.items())).encode() + image._data).hexdigest()


def decode_plane(image):
    """固定版本 pypdf 的颜色空间解码；PDFium 补充它未支持的滤镜。"""
    from copy import copy
    from pypdf.generic._image_xobject import _xobj_to_image
    plane = copy(image)
    plane.pop("/SMask", None)
    plane.pop("/Mask", None)
    try:
        return _xobj_to_image(plane)[2]
    except Exception:
        import pypdfium2 as pdfium
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with PdfWriter() as writer:
            page = writer.add_blank_page(width=1, height=1)
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im"): writer._add_object(plane)})})
            content = DecodedStreamObject()
            content.set_data(b"/Im Do")
            page[NameObject("/Contents")] = writer._add_object(content)
            output = io.BytesIO()
            writer.write(output)
        with pdfium.PdfDocument(output.getvalue()) as document:
            with closing(document[0]) as native_page:
                obj = next(native_page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                with closing(obj.get_bitmap()) as bitmap:
                    check_size(bitmap.width, bitmap.height)
                    return bitmap.to_pil().copy()


def resize_plane(image, size):
    """中心采样的 smoothstep 插值；逐轴使用 8 位权重和整数舍入。"""
    if image.size == size:
        return image
    import numpy as np
    from PIL import Image
    pixels = np.asarray(image, dtype=np.uint32)
    for axis, length in ((1, size[0]), (0, size[1])):
        source = pixels.shape[axis]
        if source == length:
            continue
        position = (np.arange(length) + 0.5) * source / length - 0.5
        left = np.floor(position).astype(int)
        fraction = position - left
        weight = np.floor(fraction * fraction * (3 - 2 * fraction) * 256 + 0.5).astype(np.uint32)
        shape = [1] * pixels.ndim
        shape[axis] = length
        weight = weight.reshape(shape)
        low_index, high_index = np.clip(left, 0, source - 1), np.clip(left + 1, 0, source - 1)
        if axis == 0 and image.width == 1 and source > 1:
            # 旧单列缩放的双行缓冲按奇偶行取样；保留其确定的 OCR 输入像素。
            swap = (left >= 0) & (left < source - 1) & (left % 2 == 1)
            low_index, high_index = np.where(swap, high_index, low_index), np.where(swap, low_index, high_index)
            if source % 2 == 1:
                low_index = np.where(left >= source - 1, source - 2, low_index)
                high_index = np.where(left >= source - 1, source - 2, high_index)
        low = np.take(pixels, low_index, axis=axis)
        high = np.take(pixels, high_index, axis=axis)
        pixels = (low * (256 - weight) + high * weight + 128) // 256
    return Image.fromarray(pixels.astype(np.uint8))


def apply_alpha(base, alpha):
    """保留旧 OCR 输入的预乘／反预乘整数舍入，包括完全透明像素。"""
    import numpy as np
    from PIL import Image
    rgb = np.asarray(base.convert("RGB"), dtype=np.uint32)
    a = np.asarray(alpha, dtype=np.uint32)[..., None]
    product = rgb * a + 128
    premultiplied = (product + (product >> 8)) >> 8
    reciprocal = (255 * 256) // np.maximum(a, 1)
    rgb = (premultiplied * reciprocal + 128) // 256
    return Image.fromarray(np.concatenate((rgb, a), axis=2).astype(np.uint8))


def image_data(image):
    from PIL import Image
    def dimensions(plane):
        size = int(plane["/Width"]), int(plane["/Height"])
        check_size(*size)
        filters = plane.get("/Filter", [])
        filters = filters if isinstance(filters, list) else [filters]
        if any(f in {"/DCTDecode", "/JPXDecode"} for f in filters):
            # 编码图像头可能与 PDF 字典尺寸不符；Pillow.open 在此不解码像素。
            with Image.open(io.BytesIO(plane.get_data())) as header:
                size = header.size
                check_size(*size)
        return size
    width, height = dimensions(image)
    mask = image.get("/SMask") or image.get("/Mask")
    mask = mask.get_object() if mask is not None else None
    if hasattr(mask, "get_data"):
        mw, mh = dimensions(mask)
        width, height = max(width, mw), max(height, mh)
        check_size(width, height)
    base = decode_plane(image)
    check_size(*base.size)
    if hasattr(mask, "get_data"):
        alpha = decode_plane(mask).convert("L")
        check_size(*alpha.size)
        target = (max(base.width, alpha.width), max(base.height, alpha.height))
        check_size(*target)
        base = resize_plane(base.convert("RGB"), target)
        alpha = resize_plane(alpha, target)
        if image.get("/SMask") is None:
            from PIL import ImageOps
            alpha = ImageOps.invert(alpha)
        base = apply_alpha(base, alpha)
    elif isinstance(mask, list):
        # Color-key 蒙版的上下界按原色分量比较。
        alpha = Image.new("L", base.size)
        alpha.putdata([0 if all(mask[2*i] <= v <= mask[2*i+1] for i, v in enumerate(pixel if isinstance(pixel, tuple) else (pixel,))) else 255
                       for pixel in base.getdata()])
        base = base.convert("RGB")
        base.putalpha(alpha)
    if base.mode not in {"RGB", "RGBA", "L", "LA"}:
        base = base.convert("RGB")
    output = io.BytesIO()
    base.save(output, "PNG")
    return output.getvalue()


def text_characters(page, check):
    """逐字符读取完整 Unicode（GetText 的 UCS-2 接口会丢失非 BMP 字符）。"""
    import pypdfium2.raw as raw
    characters = []
    with closing(page.get_textpage()) as textpage:
        for index in range(textpage.count_chars()):
            if index % 256 == 0:
                check()
            code = raw.FPDFText_GetUnicode(textpage, index)
            if not code or chr(code) in "\r\n":
                continue
            flags = ctypes.c_int()
            size = raw.FPDFText_GetFontInfo(textpage, index, None, 0, flags)
            font = ctypes.create_string_buffer(size)
            raw.FPDFText_GetFontInfo(textpage, index, font, size, flags)
            colors = [ctypes.c_uint() for _ in range(4)]
            raw.FPDFText_GetFillColor(textpage, index, *colors)
            x, y = ctypes.c_double(), ctypes.c_double()
            raw.FPDFText_GetCharOrigin(textpage, index, x, y)
            matrix = raw.FS_MATRIX()
            raw.FPDFText_GetMatrix(textpage, index, matrix)
            length = math.hypot(matrix.a, matrix.b)
            generated = bool(raw.FPDFText_IsGenerated(textpage, index))
            direction = (round(matrix.a / length, 6), round(matrix.b / length, 6)) if length else (1, 0)
            if generated and characters:
                direction = characters[-1]["direction"]
            char = {"c": chr(code), "font": font.value.decode("utf-8", "replace"),
                    "alpha": colors[3].value, "bbox": textpage.get_charbox(index),
                    "generated": generated, "direction": direction,
                    "x": x.value, "y": y.value, "size": raw.FPDFText_GetFontSize(textpage, index)}
            # ToUnicode 的 UTF-16 映射在部分 PDF 中按两个字符返回。
            if characters and 0xD800 <= ord(characters[-1]["c"]) <= 0xDBFF and 0xDC00 <= code <= 0xDFFF:
                previous = characters[-1]
                previous["c"] = chr(0x10000 + ((ord(previous["c"]) - 0xD800) << 10) + code - 0xDC00)
                a, b, c, d = previous["bbox"]
                e, f, g, h = char["bbox"]
                previous["bbox"] = (min(a, e), min(b, f), max(c, g), max(d, h))
            else:
                characters.append(char)
    return characters


def page_text(characters, candidates, number, warnings):
    from functools import cmp_to_key
    removed, lines = False, []
    # 沿文字基线排序，按垂直于基线的位置分行；相交行保留绘制顺序。
    for char in characters:
        if char["generated"] and lines:
            # PDFium 补出的空白位置可能未旋转，跟随前一个字形，不另起一行。
            if lines[-1][2]:
                lines[-1][2].append((lines[-1][2][-1][0], char["c"]))
            continue
        dx, dy = char["direction"]
        baseline = char["y"] * dx - char["x"] * dy
        position = char["x"] * dx + char["y"] * dy
        if (not lines or lines[-1][3] != char["direction"]
                or abs(lines[-1][0] - baseline) > max(1, char["size"] * 0.2)):
            lines.append((baseline, char["size"], [], char["direction"], char["y"]))
        text = char["c"]
        if char["font"].split("+")[-1].lstrip("/") in {"Symbol", "SymbolMT"}:
            text = text.translate(SYMBOL_TRANSLATION)
        code = ord(text)
        private = 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0xFFFFD or 0x100000 <= code <= 0x10FFFD
        if private and char["alpha"] == 0:
            x0, y0, x1, y1 = char["bbox"]
            x, y = (x0 + x1) / 2, (y0 + y1) / 2
            if any(a <= x <= c and b <= y <= d for a, b, c, d in candidates):
                removed = True
                continue
        lines[-1][2].append((position, text))
    if removed:
        warnings.append(f"第 {number} 页：已忽略与图片重合的隐藏异常字符，相关符号可能未完整恢复，请对照原文件。")
    def compare(a, b):
        first, second = (a[0], b[0]) if a[3] == b[3] else (a[4], b[4])
        if abs(first - second) > max(a[1], b[1]) * 0.5:
            return -1 if first > second else 1
        return 0
    return "\n".join("".join(text for _, text in sorted(chars, key=lambda c: c[0])).strip()
                     for _, _, chars, _, _ in sorted(lines, key=cmp_to_key(compare))).strip()


def decoded_images(candidates, check):
    """逐张解码，yield 前已释放原生锁；OCR 不积存整页像素，也不持锁。"""
    attempted = set()
    for image, box in candidates:
        check()
        identity = image_identity(image)
        if identity in attempted:
            continue
        attempted.add(identity)
        try:
            with pdfium_lock(check):
                raw = image_data(image)
        except (AppError, TaskInterrupted):
            raise
        except Exception:
            raw = None
        yield {"raw": raw, "bbox": box}


class PdfDocument:
    def __init__(self, path, check=lambda: None, max_pages=10_000):
        from pypdf import PdfReader
        self.check = check
        self.check()
        self.raw = path.read_bytes()
        if not self.raw.startswith(b"%PDF-"):
            raise ValueError("not a PDF")
        self.reader = PdfReader(io.BytesIO(self.raw))
        if self.reader.is_encrypted:
            self.reader.close()
            raise AppError("encrypted_document", "暂不支持加密 PDF，请解除密码后上传。")
        self.page_count = len(self.reader.pages)
        if self.page_count > max_pages:
            self.reader.close()
            raise AppError("document_limit", "PDF 页数超过 10,000 页，请拆分后上传。")
        self.native_raw = None

    def native_text_data(self):
        if self.native_raw is not None:
            return self.native_raw
        # SymbolMT 是标准 Symbol 的别名；未嵌入时 PDFium 的替代字体字宽不同。
        # 仅在内存副本中取得标准字体的位置，Unicode、字体名和透明度仍读原件。
        from pypdf import PdfWriter
        from pypdf.generic import NameObject
        fonts, seen = [], set()
        def visit(resources):
            if id(resources) in seen:
                return
            seen.add(id(resources))
            for font in resources.get("/Font", {}).values():
                font = font.get_object()
                if font.get("/Subtype") == "/Type1" and "/FontDescriptor" not in font:
                    if str(font.get("/BaseFont", "")).split("+")[-1].lstrip("/") == "SymbolMT":
                        fonts.append(font)
            for obj in resources.get("/XObject", {}).values():
                obj = obj.get_object()
                if obj.get("/Subtype") == "/Form":
                    visit(obj.get("/Resources", resources))
        for page in self.reader.pages:
            self.check()
            visit(page.get("/Resources", {}))
        self.native_raw = self.raw
        if fonts:
            writer = PdfWriter(clone_from=self.reader)
            try:
                for obj in writer._objects:
                    if isinstance(obj, dict) and obj.get("/Subtype") == "/Type1" and "/FontDescriptor" not in obj:
                        if str(obj.get("/BaseFont", "")).split("+")[-1].lstrip("/") == "SymbolMT":
                            obj[NameObject("/BaseFont")] = NameObject("/Symbol")
                output = io.BytesIO()
                writer.write(output)
                self.native_raw = output.getvalue()
            finally:
                writer.close()
        return self.native_raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.reader.close()

    def read_page(self, index, text=True):
        self.check()
        page, warnings = self.reader.pages[index], []
        try:
            candidates = drawn_images(page, self.reader, self.check)
        except (AppError, TaskInterrupted):
            raise
        except Exception:
            candidates = []
            warnings.append(f"第 {index + 1} 页：图片信息无法读取，可能遗漏图片文字。")
        # 内容流检查独立于图片枚举，即使枚举失败也不放宽限制。
        content_data(page.get_contents())
        body = ""
        if text:
            with pdfium_lock(self.check):
                import pypdfium2 as pdfium
                with pdfium.PdfDocument(self.raw) as document:
                    with closing(document[index]) as native_page:
                        chars = text_characters(native_page, self.check)
                native_data = self.native_text_data()
                if native_data is not self.raw:
                    with pdfium.PdfDocument(native_data) as document:
                        with closing(document[index]) as native_page:
                            positions = text_characters(native_page, self.check)
                    source = [c for c in chars if not c["generated"]]
                    target = [c for c in positions if not c["generated"]]
                    if len(source) == len(target):
                        for original, position in zip(source, target):
                            if original["font"].split("+")[-1].lstrip("/") == "SymbolMT":
                                original.update({key: position[key] for key in ("bbox", "x", "y", "size")})
                body = page_text(chars, [box for _, box in candidates], index + 1, warnings)
        return {"number": index + 1, "text": body, "images": decoded_images(candidates, self.check), "warnings": warnings}

    def write_pages(self, first, last):
        from pypdf import PdfWriter
        writer = PdfWriter()
        try:
            for index in range(first, last):
                self.check()
                content_data(self.reader.pages[index].get_contents())
                writer.add_page(self.reader.pages[index])
            writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
            for page in writer.pages:
                page.compress_content_streams()
            output = io.BytesIO()
            writer.write(output)
            return output.getvalue()
        finally:
            writer.close()


def image_entries(page):
    seen, index = set(), 0
    for image in page["images"]:
        raw = image["raw"]
        if raw is None:
            index += 1
            yield index, None, None
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        index += 1
        yield index, raw, digest


def page_images(page, images, cache_keys=None):
    records = []
    for index, raw, digest in image_entries(page):
        images.check()
        if raw is None:
            images.warnings.append(f"第 {page['number']} 页 · 图片{index}：图片格式无法读取，未识别其中内容。")
            continue
        key = cache_keys.get(digest) if cache_keys else None
        text = images.read(raw, f"第 {page['number']} 页 · 图片{index}", cache_key=key)
        records.append({"page": page["number"], "image": index, "digest": key or digest, "text": text})
    return page["images"], records


# 映射来源：https://www.unicode.org/Public/MAPPINGS/VENDORS/ADOBE/symbol.txt
#
# 名称：Adobe Symbol 编码到 Unicode 的映射
# Unicode 版本：2.0
# 映射表版本：1.0
# 日期：2011 年 7 月 12 日
# 下方为随映射数据附带的原始版权与授权声明，按其要求保留原文。
#
# Copyright (c) 1991-2011 Unicode, Inc. All Rights reserved.
#
# This file is provided as-is by Unicode, Inc. (The Unicode Consortium). No
# claims are made as to fitness for any particular purpose. No warranties of
# any kind are expressed or implied. The recipient agrees to determine
# applicability of information provided. If this file has been provided on
# magnetic media by Unicode, Inc., the sole remedy for any claim will be
# exchange of defective media within 90 days of receipt.
#
# Unicode, Inc. hereby grants the right to freely use the information
# supplied in this file in the creation of products supporting the
# Unicode Standard, and to make copies of this file in any form for
# internal or external distribution as long as this notice remains
# attached.
ADOBE_SYMBOL = (
    '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    '\x00!∀#∃%&∋()∗+,−./0123456789:;<=>?'
    '≅ΑΒΧ∆ΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_'
    '\x00αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼\x00'
    '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    '€ϒ′≤∕∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…\x00\x00↵'
    'ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇\x00\x00\x00∏√⋅¬∧∨⇔⇐⇑⇒⇓'
    '◊〈\x00\x00\x00∑\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00〉∫⌠\x00⌡\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
)
SYMBOL_TRANSLATION = {0xF000 + index: char for index, char in enumerate(ADOBE_SYMBOL) if char != "\0"}
