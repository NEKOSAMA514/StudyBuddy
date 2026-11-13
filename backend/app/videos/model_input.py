"""只精简模型发送副本；重复 OCR 直接省略，原始识别和依据保留。"""
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from itertools import accumulate


OCR_REPEAT_SECONDS = 2
SLIDE_REPEAT_SECONDS = 30
SPEECH_LINK_SECONDS = 1


def _speech_at_frames(frames, records, tolerance):
    speech = sorted((item for item in records if item["kind"] == "speech" and
                    "start_seconds" in item and "end_seconds" in item),
                    key=lambda item: item["start_seconds"])
    starts = [item["start_seconds"] for item in speech]
    # 保留跨过较短语音片段的长片段，同时限制每帧要比较的时间范围。
    ends = list(accumulate((item["end_seconds"] for item in speech), max))
    for frame in frames:
        t = frame["start_seconds"]
        left, right = bisect_left(ends, t - tolerance), bisect_right(starts, t + tolerance)
        yield frame, [item for item in speech[left:right] if item["end_seconds"] >= t - tolerance]


def _normalized(text):
    return re.sub(r'[\s，,。；;：:、！？!?“”"‘’]', "", text)


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
                    literal = any(char.isdigit() for char in text) or re.search(r'[+\-−=<>%\\^_{}\[\]()/]', text)
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
                minimum = 6 if gap <= OCR_REPEAT_SECONDS else 12
                repeats = {line for line, count in current.items()
                           if previous_lines[line] == count and len(_normalized(line)) >= minimum}
                if gap <= OCR_REPEAT_SECONDS or (len(repeats) >= 2 and
                        2 * sum(map(len, repeats)) >= sum(map(len, current))):
                    text = "".join(line for line in visible if line.strip() not in repeats).strip("\r\n")
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


def outline_text_length(payload):
    """统计实际发送的材料与内容类型文字，已省略的重复 OCR 不再计入。"""
    compact = deduplicate_ocr(payload)
    return len(compact.get("content_type", "")) + sum(len(item["text"]) for key in ("evidence", "primary", "context")
               for item in compact.get(key, []))
