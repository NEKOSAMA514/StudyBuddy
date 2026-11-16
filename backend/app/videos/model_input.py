"""只精简模型发送副本；重复 OCR 直接省略，原始识别和依据保留。"""
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from itertools import accumulate


OCR_REPEAT_SECONDS = 2
SLIDE_REPEAT_SECONDS = 30
SPEECH_LINK_SECONDS = 1
SPEECH_BLOCK_SECONDS = 60


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


def _without_repeated_lines(lines, current, previous, gap, *, preserve_details=False):
    minimum = 6 if gap <= OCR_REPEAT_SECONDS and not preserve_details else 12
    repeats = {line for line, count in current.items()
               if previous[line] == count and len(_normalized(line)) >= minimum
               and not (preserve_details and _requires_literal_match(line))}
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
            identity = (item["id"], item["text"])
            if key == "context" and identity in primary_frames:
                continue
            if key != "context":
                primary_frames.add(identity)
            start = item.get("start_seconds")
            gap = start - previous_time if start is not None and previous_time is not None else float("inf")
            spoken = [record["text"] for record in overlapping.get(item["id"], [])]
            normalized_speech = [_normalized(text) for text in spoken]
            visible = []
            for line in item["text"].splitlines(keepends=True):
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


def _without_spoken_lines(text, speech):
    runs, last_end = [], float("-inf")
    for item in speech:
        if item["start_seconds"] - last_end > SPEECH_LINK_SECONDS:
            runs.append([])
        runs[-1].append(item["text"].strip())
        last_end = max(last_end, item["end_seconds"])
    spoken = [" ".join(run) for run in runs]
    normalized_speech = [_normalized(phrase) for phrase in spoken]
    retained = []
    for line in text.splitlines(keepends=True):
        raw, plain = line.strip(), _normalized(line)
        if len(plain) >= 12:
            candidate, phrases = (raw, spoken) if _requires_literal_match(raw) else (plain, normalized_speech)
            if any(candidate in phrase for phrase in phrases):
                continue
        retained.append(line)
    return "".join(retained).strip("\r\n")


def _chapter_frame_texts(items, speech, seen_frames):
    speech_at = _overlap_lookup(speech)
    previous_end, previous_lines = None, Counter()
    for item in items:
        if item["kind"] != "frame" or item["id"] in seen_frames:
            continue
        seen_frames.add(item["id"])
        start, end = item.get("start_seconds"), item.get("end_seconds")
        timed = start is not None and end is not None
        text = _without_spoken_lines(item["text"], speech_at(start, end)) if timed else item["text"]
        if not text.strip():
            continue
        lines = text.splitlines(keepends=True)
        current = Counter(line.strip() for line in lines)
        if timed and previous_end is not None:
            gap = max(0, start - previous_end)
            if gap <= OCR_REPEAT_SECONDS and current != previous_lines:
                # 连续画面只压缩共同的长段文字，每次出现仍保留自己的原始时段。
                remaining = _without_repeated_lines(lines, current, previous_lines, gap, preserve_details=True)
                text = "".join(remaining).strip("\r\n") or text
        previous_end, previous_lines = end if timed else None, current
        yield item, text


def _pack_chapter_primary(records, sources, seen_frames):
    items = sorted(records, key=_start_order)
    speech = [item for item in items if item["kind"] == "speech" and item["text"].strip()]
    headings = {}
    for number, block in enumerate(_speech_blocks(speech), 1):
        identifier = f"New_S{number}"
        fragments = block["fragments"]
        headings[fragments[0]["id"]] = f"[{identifier}]"
        sources[identifier] = fragments

    timed_speech = [cue for cue in speech if cue.get("start_seconds") is not None and cue.get("end_seconds") is not None]
    speech_starts = [cue["start_seconds"] for cue in timed_speech]
    frames_at = defaultdict(list)
    for number, (item, text) in enumerate(_chapter_frame_texts(items, speech, seen_frames), 1):
        identifier = f"F{number}"
        sources[identifier] = [item]
        # 画面出现在最近一句中时放到句前，停顿中的画面保留自己的时间顺序。
        start = item.get("start_seconds")
        index = bisect_right(speech_starts, start) - 1 if start is not None else -1
        anchor = item["id"]
        if index >= 0 and start <= timed_speech[index]["end_seconds"]:
            anchor = timed_speech[index]["id"]
        first, *rest = text.splitlines(keepends=True)
        continuation = "".join(f"> {line}" for line in rest)
        frames_at[anchor].append(f"> [{identifier}] {first}{continuation}")

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


def _chapter_context_text(records, seen_frames):
    items = sorted(records, key=_start_order)
    speech = [item for item in items if item["kind"] == "speech" and item["text"].strip()]
    frames = {item["id"]: text for item, text in _chapter_frame_texts(items, speech, seen_frames)}
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
    sent = {key: value for key, value in chapter.items() if key not in {"start_seconds", "end_seconds"}}
    sources, seen_frames = {}, set()
    crossing, preceding = split_chapter_context(chapter)
    if "primary" in chapter or crossing:
        sent["primary"] = _pack_chapter_primary([*crossing, *chapter.get("primary", [])], sources, seen_frames)
    if "context" in chapter:
        sent["context"] = _chapter_context_text(preceding, seen_frames)
    return sent, sources


def outline_text_length(payload):
    """统计实际发送的材料与内容类型文字，已省略的重复 OCR 不再计入。"""
    compact = deduplicate_ocr(payload)
    return len(compact.get("content_type", "")) + sum(len(item["text"]) for key in ("evidence", "primary", "context")
               for item in compact.get(key, []))
