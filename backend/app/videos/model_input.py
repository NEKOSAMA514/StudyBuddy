"""只精简模型发送副本；重复 OCR 直接省略，原始识别和依据保留。"""
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from itertools import accumulate, groupby
from string import punctuation


OCR_REPEAT_SECONDS = 2
SLIDE_REPEAT_SECONDS = 30
SPEECH_LINK_SECONDS = 1
SPEECH_BLOCK_SECONDS = 60
MIN_OCR_TEXT_MATCH = 5
NEGATIONS = "不没未无非别勿否"
FORMULA_SYMBOLS = r"+-−±×÷=<>≤≥≠%‰\^_{}[]()/*°℃℉"
EXPRESSION_DELIMITERS = ".,:。!?;；、…“”‘’\"'"
# AgCl 等只含字母的式子也需要完整匹配。
ELEMENT_SEQUENCE = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?){2,}(?![A-Za-z])")
# 同一个标点或符号的全角变体逐个对应 ASCII。
OCR_SYMBOLS = {ord(symbol) + 0xFEE0: symbol for symbol in punctuation}
OCR_SENTENCE_END = re.compile(r"(?<=。)|(?<=[\u3400-\u9fff][!?])|(?<=[!?])(?=[\u3400-\u9fff])")


def _ocr_line_endings(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def ocr_request_text(text):
    return text.replace("\r", "").replace("\n", "")


def evidence_request_text(item):
    return ocr_request_text(item["text"]) if item.get("kind") == "frame" else item["text"]


def outline_source_aliases(records):
    """为当前大纲请求分配来源编号，保留本地证据编号的映射。"""
    counts, aliases = Counter(), {}
    for item in records:
        prefix = "F" if item.get("kind") == "frame" else "S"
        counts[prefix] += 1
        aliases[item["id"]] = f"{prefix}{counts[prefix]}"
    return aliases


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
    return any(char.isdigit() or char in FORMULA_SYMBOLS for char in text) or ELEMENT_SEQUENCE.search(text) is not None


def _expression_edge(char):
    return (char.isdigit() or char in FORMULA_SYMBOLS + "." or
            (char.isalpha() and not "\u3400" <= char <= "\u9fff"))


def _literal_in_speech(candidate, speech):
    if candidate not in speech:
        return False
    starts_expression = _expression_edge(candidate[0])
    ends_expression = _expression_edge(candidate[-1]) or candidate[-1] in ",:"
    pattern = rf"(?<![{NEGATIONS}]){re.escape(candidate)}"
    for match in re.finditer(pattern, speech):
        before = speech[:match.start()].rpartition("\n")[2].rstrip()
        after = speech[match.end():].partition("\n")[0].lstrip()
        # 空格、千位分隔和单位可能仍属于同一表达式，只有明确的边界才允许去重。
        if starts_expression and before and (
                _expression_edge(before[-1]) or before.endswith(("正", "负")) or re.search(r"\d[^\S\r\n]*[,:]$", before)):
            continue
        if ends_expression and after and (after[0] not in EXPRESSION_DELIMITERS or re.match(r"[.,:][^\S\r\n]*\d", after)):
            continue
        return True
    return False


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
            spoken = [_ocr_line_endings(record["text"]).translate(OCR_SYMBOLS)
                      for record in overlapping.get(item["id"], [])]
            normalized_speech = [_normalized(text) for text in spoken]
            visible = []
            for line in frame_text.splitlines(keepends=True):
                text = line.strip()
                if spoken and len(plain := _normalized(text)) >= 6:
                    # 数字和公式只作原文匹配，保留符号与小数点。
                    literal = _requires_literal_match(text)
                    candidate, voices = (text, spoken) if literal else (plain, normalized_speech)
                    # 短标签碰巧出现在同期口播时，仍保留对应画面。
                    if len(plain) < 12:
                        repeated = candidate in voices
                    elif literal:
                        repeated = any(_literal_in_speech(candidate, voice) for voice in voices)
                    else:
                        repeated = any(candidate in voice for voice in voices)
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


def _spoken_duplicate(raw, plain, spoken, normalized_speech):
    literal = _requires_literal_match(raw)
    if len(plain) < (12 if literal else MIN_OCR_TEXT_MATCH):
        return False
    if literal:
        candidate = re.sub(r"。+$|(?<=[\u3400-\u9fff])[!?]+$", "", raw.strip())
        return _literal_in_speech(candidate, spoken)
    return plain in normalized_speech and bool(re.search(rf"(?<![{NEGATIONS}]){re.escape(plain)}", normalized_speech))


def _ocr_text_parts(text):
    return [(row, part) for row, line in enumerate(text.split("\n"))
            for part in OCR_SENTENCE_END.split(line) if part or not line]


def _join_ocr_parts(parts):
    return "\n".join("".join(text for _, text in line) for _, line in groupby(parts, key=lambda part: part[0]))


def _short_ocr_label(raw):
    return raw.isalpha() and (len(raw) <= 3 or (raw.isascii() and len(raw) < 12))


def _without_spoken_lines(text, spoken, normalized_speech):
    longest = len(normalized_speech) + 1
    parts = _ocr_text_parts(text)
    # 独立成行的短词可能是角色或图表名牌，保留它们的出现位置。
    labels = [_short_ocr_label("".join(part.split())) for _, part in parts]
    retained = []
    index = 0
    while index < len(parts):
        raw, last_match = "", index
        for end in range(index, len(parts)):
            if labels[end]:
                break
            row, part = parts[end]
            raw += part
            if row != parts[index][0] and _requires_literal_match(raw):
                break
            plain = _speech_comparison_text(raw)
            if len(plain) > longest:
                break
            if _spoken_duplicate(raw, plain, spoken, normalized_speech):
                last_match = end + 1
        if last_match == index:
            retained.append(parts[index])
        index = max(index + 1, last_match)
    return _join_ocr_parts(retained).strip("\n")


def _chapter_frame_texts(items, speech_blocks, seen_frames, clipped_lines):
    blocks_at = _overlap_lookup(speech_blocks)
    speech_texts = {}
    for block in speech_blocks:
        spoken = _ocr_line_endings("\n".join(fragment["text"] for fragment in block["fragments"])).translate(OCR_SYMBOLS)
        speech_texts[block["fragments"][0]["id"]] = (spoken, _speech_comparison_text(spoken))
    previous_parts = {}
    available = {item["id"] for item in items} - seen_frames if clipped_lines else set()
    for item in items:
        if item["kind"] != "frame" or item["id"] in seen_frames:
            continue
        seen_frames.add(item["id"])
        start, end = item.get("start_seconds"), item.get("end_seconds")
        timed = start is not None and end is not None
        text = _ocr_line_endings(item["text"]).translate(OCR_SYMBOLS)
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
            for part, count in Counter(part.strip() for _, part in parts).items():
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
                text = _join_ocr_parts((row, part) for row, part in parts if part.strip() not in repeated).strip("\n")
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
