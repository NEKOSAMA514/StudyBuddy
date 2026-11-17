"""只精简模型发送副本；重复 OCR 直接省略，原始识别和依据保留。"""
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from itertools import accumulate
from string import punctuation


OCR_REPEAT_SECONDS = 2
SLIDE_REPEAT_SECONDS = 30
SPEECH_LINK_SECONDS = 1
SPEECH_BLOCK_SECONDS = 60
MIN_OCR_TEXT_MATCH = 5
NEGATIONS = "不没未无非别勿否"
SIGNIFICANT_HAN = NEGATIONS + "正负〇零一二三四五六七八九十百千万亿两半"
# 同一个标点或符号的全角变体逐个对应 ASCII。
OCR_SYMBOLS = {ord(symbol) + 0xFEE0: symbol for symbol in punctuation}
OCR_SENTENCE_END = re.compile(r"(?<=。)|(?<=[\u3400-\u9fff][!?])|(?<=[!?])(?=[\u3400-\u9fff])")


def ocr_request_text(text):
    return text.replace("\r", "").replace("\n", "")


def evidence_request_text(item):
    return ocr_request_text(item["text"]) if item.get("kind") == "frame" else item["text"]


def quote_source_text(item):
    text = evidence_request_text(item)
    return text.translate(OCR_SYMBOLS) if item.get("kind") == "frame" else text


def find_source_quote(quote, sources):
    for item in sources:
        candidate = ocr_request_text(quote).translate(OCR_SYMBOLS) if item.get("kind") == "frame" else quote
        if candidate and candidate in quote_source_text(item):
            return candidate
    return None


def _start_order(item):
    start = item.get("start_seconds")
    return (start is None, start)


def _overlap_lookup(records):
    timed = sorted((item for item in records if item.get("start_seconds") is not None and
                    item.get("end_seconds") is not None), key=_start_order)
    starts = [item["start_seconds"] for item in timed]
    # 即使短片段落在长片段内，也保留仍有时间交集的长片段。
    ends = list(accumulate((item["end_seconds"] for item in timed), max))

    def overlapping(start, end):
        left, right = bisect_left(ends, start), bisect_right(starts, end)
        return [item for item in timed[left:right] if item["end_seconds"] >= start]

    return overlapping


def _speech_at_frames(frames, records, tolerance):
    overlapping = _overlap_lookup(item for item in records if item["kind"] == "speech")
    for frame in frames:
        t = frame["start_seconds"]
        yield frame, overlapping(t - tolerance, t + tolerance)


def _normalized(text):
    return re.sub(r'[\s，,。；;：:、！？!?“”"‘’]', "", text)


def _requires_literal_match(text):
    return any(char.isdigit() for char in text) or re.search(r'[+\-−=<>%\\^_{}\[\]()/]', text)


def _without_repeated_lines(lines, current, previous, gap):
    minimum = 6 if gap <= OCR_REPEAT_SECONDS else 12
    repeats = {line for line, count in current.items()
               if previous[line] == count and len(_normalized(line)) >= minimum}
    if gap <= OCR_REPEAT_SECONDS or (len(repeats) >= 2 and
            2 * sum(map(len, repeats)) >= sum(map(len, current))):
        return [line for line in lines if line.strip() not in repeats]
    return lines


# 只折叠短时间连续画面中的重复行，以及同一时刻已经出现在转录中的字幕。
def deduplicate_ocr(payload):
    if "chapters" in payload:
        return {**payload, "chapters": [deduplicate_ocr(chapter) for chapter in payload["chapters"]]}
    if "chapter" in payload:
        return {**payload, "chapter": deduplicate_ocr(payload["chapter"])}
    keys = [key for key in ("evidence", "primary", "context") if key in payload]
    if not keys:
        return payload

    result, primary_frames = dict(payload), set()
    for key in keys:
        items = payload[key]
        frames = [item for item in items if item["kind"] == "frame" and "start_seconds" in item]
        overlapping = {frame["id"]: speech for frame, speech in _speech_at_frames(frames, items, 0)} if frames else {}
        previous_time, previous_text, previous_lines = None, None, Counter()
        retained = []
        for item in items:
            if item["kind"] != "frame":
                retained.append(item)
                continue
            frame_text = item["text"].translate(OCR_SYMBOLS)
            identity = (item["id"], frame_text)
            if key == "context" and identity in primary_frames:
                continue
            if key != "context":
                primary_frames.add(identity)
            start = item.get("start_seconds")
            gap = start - previous_time if start is not None and previous_time is not None else float("inf")
            spoken = [record["text"].translate(OCR_SYMBOLS) for record in overlapping.get(item["id"], [])]
            normalized_speech = [_normalized(text) for text in spoken]
            visible = []
            for line in frame_text.splitlines(keepends=True):
                text = line.strip()
                if spoken and len(plain := _normalized(text)) >= 6:
                    # 数字和公式只作原文匹配，保留符号与小数点。
                    literal = _requires_literal_match(text)
                    candidate, voices = (text, spoken) if literal else (plain, normalized_speech)
                    # 短标签碰巧出现在同期口播时，仍保留对应画面。
                    repeated = (candidate in voices if len(plain) < 12 else
                                any(candidate in voice for voice in voices))
                    if repeated:
                        continue
                visible.append(line)
            visible_text = "".join(visible).strip("\r\n")
            current = Counter(line.strip() for line in visible)
            text = visible_text
            if 0 <= gap <= OCR_REPEAT_SECONDS and visible_text == previous_text:
                text = ""
            elif 0 <= gap <= SLIDE_REPEAT_SECONDS:
                # 同一帧内的重复行成组处理，保留出现次数变化的表格与矩阵行。
                # 稀疏采样时，多行长文字相同才视为同一页的持续画面。
                text = "".join(_without_repeated_lines(visible, current, previous_lines, gap)).strip("\r\n")
            previous_time, previous_text, previous_lines = start, visible_text, current
            if text.strip():
                retained.append({**item, "text": text})
        result[key] = retained
    if "anchors" in payload and "evidence" in payload:
        retained_ids = {item["id"] for item in result["evidence"]}
        result["anchors"] = [item for item in payload["anchors"] if item["id"] in retained_ids]
    return result


def nearby_speech_ids(primary, context):
    frames = [item for item in primary if item["kind"] == "frame" and "start_seconds" in item]
    if not frames:
        return {}
    links = {}
    for frame, nearby in _speech_at_frames(frames, [*primary, *context], SPEECH_LINK_SECONDS):
        if not nearby:
            continue
        t = frame["start_seconds"]
        closest = min(nearby, key=lambda item: (max(item["start_seconds"] - t, t - item["end_seconds"], 0),
                                               abs(item["start_seconds"] - t)))
        links[frame["id"]] = closest["id"]
    return links


def _speech_blocks(speech):
    blocks = []
    for item in speech:
        start, end = item.get("start_seconds"), item.get("end_seconds")
        block = blocks[-1] if blocks else None
        if (block and start is not None and end is not None and
                block["start_seconds"] is not None and block["end_seconds"] is not None and
                max(end, block["end_seconds"]) - block["start_seconds"] <= SPEECH_BLOCK_SECONDS):
            block["fragments"].append(item)
            block["end_seconds"] = max(end, block["end_seconds"])
        else:
            blocks.append({"start_seconds": start, "end_seconds": end, "fragments": [item]})
    return blocks


def _speech_comparison_text(text):
    # 英文标识或表达式内部的点保留，句末和中文词间的点用于排版。
    return re.sub(r"(?<![A-Za-z0-9])[.·]|[.·](?![A-Za-z0-9])", "", _normalized(text).replace("…", ""))


def _one_ocr_edit(candidate, phrase):
    middle = len(candidate) // 2
    if candidate[:middle] not in phrase and candidate[middle:] not in phrase:
        return False
    # 整分钟转录可以有前文和后文，只检查候选句内部的一处差异。
    edits = [edit for edit in SequenceMatcher(None, candidate, phrase, autojunk=False).get_opcodes()
             if edit[0] != "equal" and not (edit[0] == "insert" and edit[1] in (0, len(candidate)))]
    if len(edits) != 1:
        return False
    _, left, right, start, end = edits[0]
    changed = candidate[left:right] + phrase[start:end]
    return (0 < left <= right < len(candidate) and max(right - left, end - start) == 1
            and all("\u3400" <= char <= "\u9fff" and char not in SIGNIFICANT_HAN for char in changed))


def _spoken_duplicate(raw, plain, spoken, normalized_speech):
    literal = _requires_literal_match(raw)
    if len(plain) < (12 if literal else MIN_OCR_TEXT_MATCH):
        return False
    candidate = re.sub(r"。+$|(?<=[\u3400-\u9fff])[!?]+$", "", raw) if literal else plain
    phrase = spoken if literal else normalized_speech
    prefix = r"(?<![\d.+−正负-])" if literal and (candidate[0].isdigit() or candidate[0] == ".") else ""
    suffix = r"(?![\d.+−%‰]|[,，:：-]\d)" if literal and (candidate[-1].isdigit() or candidate[-1] in ".,，:：") else ""
    pattern = prefix + rf"(?<![{NEGATIONS}])" + re.escape(candidate) + suffix
    if candidate in phrase and re.search(pattern, phrase):
        return True
    return not literal and len(plain) >= 16 and _one_ocr_edit(plain, phrase)


def _ocr_text_parts(text):
    return [part for line in text.splitlines(keepends=True) for part in OCR_SENTENCE_END.split(line) if part]


def _short_ocr_label(raw):
    return raw.isalpha() and (len(raw) <= 3 or (raw.isascii() and len(raw) < 12))


def _without_spoken_lines(text, spoken, normalized_speech):
    longest = len(normalized_speech) + 1
    parts = _ocr_text_parts(text)
    compact = ["".join(part.split()) for part in parts]
    # 独立成行的短词可能是角色或图表名牌，保留它们的出现位置。
    labels = [_short_ocr_label(raw) for raw in compact]
    retained = []
    index = 0
    while index < len(parts):
        raw, last_match = "", index
        for end in range(index, len(parts)):
            if labels[end]:
                break
            raw += compact[end]
            plain = _speech_comparison_text(raw)
            if len(plain) > longest:
                break
            if _spoken_duplicate(raw, plain, spoken, normalized_speech):
                last_match = end + 1
        if last_match == index:
            retained.append(parts[index])
        index = max(index + 1, last_match)
    return "".join(retained).strip("\r\n")


def _chapter_frame_texts(items, speech_blocks, seen_frames, clipped_lines):
    blocks_at = _overlap_lookup(speech_blocks)
    speech_texts = {}
    for block in speech_blocks:
        merged = " ".join(fragment["text"] for fragment in block["fragments"])
        spoken = "".join(merged.split()).translate(OCR_SYMBOLS)
        speech_texts[block["fragments"][0]["id"]] = (spoken, _speech_comparison_text(spoken))
    previous_parts = {}
    available = {item["id"] for item in items} - seen_frames if clipped_lines else set()
    for item in items:
        if item["kind"] != "frame" or item["id"] in seen_frames:
            continue
        seen_frames.add(item["id"])
        start, end = item.get("start_seconds"), item.get("end_seconds")
        timed = start is not None and end is not None
        text = item["text"].translate(OCR_SYMBOLS)
        if clips := clipped_lines.get(item["id"]):
            # 前后完整读数必须同时在这一组材料中，跨章或仅在章前的读数不能替代。
            removed = {line for line, before, after in clips if before in available and after in available}
            if removed:
                text = "\n".join(line for index, line in enumerate(text.split("\n")) if index not in removed)
        if timed:
            text = text.strip("\r\n")
        overlapping = blocks_at(start, end) if timed else []
        block_id = None
        if overlapping:
            # 优先使用画面开始时的语音块，若从停顿延续进语音，则用首个相交块。
            block = next((block for block in reversed(overlapping) if block["start_seconds"] <= start), overlapping[0])
            block_id = block["fragments"][0]["id"]
            text = _without_spoken_lines(text, *speech_texts[block_id])
        if not text.strip():
            continue
        if timed:
            parts = _ocr_text_parts(text)
            repeated = set()
            for part, count in Counter(part.strip() for part in parts).items():
                if (len(_normalized(part)) < MIN_OCR_TEXT_MATCH
                        or _short_ocr_label("".join(part.split()))):
                    continue
                # 画面之间用统一符号后的整行比较，数值或运算符不同的行分别保留。
                key = (block_id, part)
                previous = previous_parts.get(key)
                # 逐字出现的字幕会重复带上此前的整句，即使其间插入了名牌等画面。
                if previous and previous[0] < start <= previous[1] + OCR_REPEAT_SECONDS and previous[2] == count:
                    repeated.add(part)
                previous_parts[key] = (start, end, count)
            if repeated:
                text = "".join(part for part in parts if part.strip() not in repeated).strip("\r\n")
        if text.strip():
            yield item, ocr_request_text(text)


def _pack_chapter_primary(records, sources, seen_frames, clipped_lines):
    items = sorted(records, key=_start_order)
    speech = [item for item in items if item["kind"] == "speech" and item["text"].strip()]
    blocks = _speech_blocks(speech)
    headings = {}
    for number, block in enumerate(blocks, 1):
        identifier = f"New_S{number}"
        fragments = block["fragments"]
        headings[fragments[0]["id"]] = f"[{identifier}]"
        sources[identifier] = fragments

    timed_speech = [cue for cue in speech if cue.get("start_seconds") is not None and cue.get("end_seconds") is not None]
    speech_starts = [cue["start_seconds"] for cue in timed_speech]
    frames_at = defaultdict(list)
    for number, (item, text) in enumerate(_chapter_frame_texts(items, blocks, seen_frames, clipped_lines), 1):
        identifier = f"F{number}"
        sources[identifier] = [item]
        # 画面出现在最近一句中时放到句前，停顿中的画面保留自己的时间顺序。
        start = item.get("start_seconds")
        index = bisect_right(speech_starts, start) - 1 if start is not None else -1
        anchor = item["id"]
        if index >= 0 and start <= timed_speech[index]["end_seconds"]:
            anchor = timed_speech[index]["id"]
        frames_at[anchor].append(f"> [{identifier}] {text}")

    lines, spoken = [], []
    for item in items:
        heading = headings.get(item["id"])
        frames = frames_at.pop(item["id"], ())
        if heading or frames:
            if spoken:
                lines.append(" ".join(spoken))
                spoken.clear()
            if heading:
                lines.append(heading)
            lines.extend(frames)
        if item["kind"] == "speech" and (text := item["text"].strip()):
            spoken.append(text)
    if spoken:
        lines.append(" ".join(spoken))
    return "\n".join(lines)


def split_chapter_context(chapter):
    """将章首仍可见的画面与其余章前材料分开。"""
    start = chapter.get("start_seconds")
    crossing, preceding = [], []
    for item in chapter.get("context", []):
        frame_start, frame_end = item.get("start_seconds"), item.get("end_seconds")
        if (item["kind"] == "frame" and start is not None and frame_start is not None and frame_end is not None
                and frame_start < start <= frame_end):
            crossing.append(item)
        else:
            preceding.append(item)
    return crossing, preceding


def _chapter_context_text(records, seen_frames, clipped_lines):
    items = sorted(records, key=_start_order)
    speech = [item for item in items if item["kind"] == "speech" and item["text"].strip()]
    frames = {item["id"]: text for item, text in _chapter_frame_texts(
        items, _speech_blocks(speech), seen_frames, clipped_lines)}
    parts, previous_kind = [], None
    for item in items:
        kind = item["kind"]
        text = item["text"].strip() if kind == "speech" else frames.pop(item["id"], "")
        if not text:
            continue
        if parts:
            parts.append(" " if previous_kind == kind == "speech" else "\n")
        parts.append(text)
        previous_kind = kind
    return "".join(parts)


def pack_chapter_material(chapter):
    """组装正文发送副本，按章返回短编号与原始依据的映射。"""
    sent = {key: value for key, value in chapter.items() if key not in {"start_seconds", "end_seconds", "clipped_lines"}}
    sources, seen_frames = {}, set()
    crossing, preceding = split_chapter_context(chapter)
    clipped_lines = chapter.get("clipped_lines", {})
    if "primary" in chapter or crossing:
        sent["primary"] = _pack_chapter_primary([*crossing, *chapter.get("primary", [])], sources, seen_frames, clipped_lines)
    if "context" in chapter:
        sent["context"] = _chapter_context_text(preceding, seen_frames, clipped_lines)
    return sent, sources
