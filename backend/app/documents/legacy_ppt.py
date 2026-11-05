"""读取 PowerPoint 97–2003 的当前幻灯片及备注，不扫描已删除的历史内容。

记录结构依据 Microsoft [MS-PPT]：
https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/
"""
import struct

import olefile

from ..errors import AppError


def _record(data, offset, limit=None, expected=None):
    limit = len(data) if limit is None else limit
    if offset < 0 or offset + 8 > limit:
        raise ValueError("truncated PPT record")
    options, kind, size = struct.unpack_from("<HHI", data, offset)
    start, end = offset + 8, offset + 8 + size
    if end > limit or expected is not None and kind != expected:
        raise ValueError("invalid PPT record")
    return kind, options >> 4, options & 15, start, end


def _records(data, start, end):
    while start < end:
        record = _record(data, start, end)
        yield record
        start = record[4]


# 最新持久对象优先；旧编辑记录只补齐当前版本仍引用、但尚未重写的对象。
def _objects(data, offset, check):
    objects, visited, document = {}, set(), None
    while offset:
        check()
        if offset in visited:
            raise ValueError("cyclic PPT edit chain")
        visited.add(offset)
        _, _, _, start, end = _record(data, offset, expected=4085)
        if end - start not in {28, 32}:
            raise ValueError("invalid PPT edit")
        previous, directory, reference = struct.unpack_from("<III", data, start + 8)
        if end - start == 32 and struct.unpack_from("<I", data, start + 28)[0]:
            raise AppError("invalid_document", "PPT 已加密，请解除密码保护后重新上传。")
        if document is None:
            document = reference
        kind, _, _, position, stop = _record(data, directory)
        if kind not in {6001, 6002}:
            raise ValueError("invalid PPT persist directory")
        while position < stop:
            if position + 4 > stop:
                raise ValueError("truncated PPT persist entry")
            entry = struct.unpack_from("<I", data, position)[0]
            first, count = entry & 0xFFFFF, entry >> 20
            position += 4
            if not first or not count or position + count * 4 > stop:
                raise ValueError("invalid PPT persist entry")
            for index in range(count):
                target = struct.unpack_from("<I", data, position + index * 4)[0]
                objects.setdefault(first + index, target)
            position += count * 4
            if len(objects) > 100_000:
                raise AppError("document_limit", "旧版 PPT 对象过多，请拆分或另存为 PPTX 后上传。")
        offset = previous
    return objects, document


def _texts(data, start, end, check, notes=False, depth=0):
    if depth > 64:
        raise AppError("document_limit", "旧版 PPT 结构过深，请另存为 PPTX 后上传。")
    result, text_kind = [], None
    for kind, _, version, first, stop in _records(data, start, end):
        check()
        if kind == 3999:  # TextHeaderAtom
            if stop - first != 4:
                raise ValueError("invalid PPT text header")
            text_kind = struct.unpack_from("<I", data, first)[0]
        elif kind in {4000, 4008} and (not notes or text_kind == 2):
            encoding = "utf-16-le" if kind == 4000 else "latin-1"
            text = data[first:stop].decode(encoding).replace("\r", "\n").replace("\v", "\n").replace("\x00", "").strip()
            if text:
                result.append(text)
        elif version == 15:
            result.extend(_texts(data, first, stop, check, notes, depth + 1))
    return result


# SlidePersistAtom 后的文字属于该页，页序使用列表顺序，而不是对象编号或文件偏移。
def _slides(data, start, end, max_slides):
    slides, current = [], None
    for kind, _, _, first, stop in _records(data, start, end):
        if kind != 1011:
            continue
        if stop - first != 20:
            raise ValueError("invalid PPT slide reference")
        if current:
            slides.append((*current, first - 8))
        reference = struct.unpack_from("<I", data, first)[0]
        identifier = struct.unpack_from("<I", data, first + 12)[0]
        current = reference, identifier, stop
        if len(slides) >= max_slides:
            raise AppError("document_limit", "幻灯片数量过多，请拆分后上传。")
    if current:
        slides.append((*current, end))
    return slides


def _page_text(data, objects, slide, check, notes=False):
    reference, _, first, stop = slide
    _, _, _, start, end = _record(data, objects[reference], expected=1008 if notes else 1006)
    outline = _texts(data, first, stop, check, notes)
    shapes = _texts(data, start, end, check, notes)
    # 正文可通过 OutlineTextRefAtom 在形状中引用，避免重复输出同一份文字。
    text = outline + [part for part in shapes if part not in outline]
    return "\n\n".join(text), start, end


def presentation_units(path, check, max_slides):
    with olefile.OleFileIO(path) as source:
        if not source.exists("Current User") or not source.exists("PowerPoint Document"):
            raise ValueError("not a PowerPoint 97-2003 document")
        if any(source.get_size(name) > 128 * 1024 * 1024 for name in ("Current User", "PowerPoint Document")):
            raise AppError("document_limit", "旧版 PPT 数据流过大，当前最多解析 128 MB 内容。")
        current = source.openstream("Current User").read()
        _record(current, 0, expected=4086)
        if len(current) < 20:
            raise ValueError("invalid PPT current user")
        token, offset = struct.unpack_from("<II", current, 12)
        if token == 0xF3D1C4DF:
            raise AppError("invalid_document", "PPT 已加密，请解除密码保护后重新上传。")
        if token != 0xE391C05F:
            raise ValueError("invalid PPT current user token")
        data = source.openstream("PowerPoint Document").read()
    objects, document = _objects(data, offset, check)
    _, _, _, start, end = _record(data, objects[document], expected=1000)
    lists = {}
    for kind, instance, _, first, stop in _records(data, start, end):
        if kind == 4080 and instance in {0, 2}:
            if instance in lists:
                raise ValueError("duplicate PPT slide list")
            lists[instance] = _slides(data, first, stop, max_slides)
    notes = {slide[1]: slide for slide in lists.get(2, [])}
    for number, slide in enumerate(lists.get(0, []), 1):
        check()
        text, first, stop = _page_text(data, objects, slide, check)
        for kind, _, _, body, end in _records(data, first, stop):
            if kind == 1007 and end - body >= 20:
                note = notes.get(struct.unpack_from("<I", data, body + 16)[0])
                if note:
                    content, _, _ = _page_text(data, objects, note, check, notes=True)
                    if content:
                        text += "\n\n讲者备注：\n" + content
        yield text.strip(), f"幻灯片 {number}", {"slide": number}
