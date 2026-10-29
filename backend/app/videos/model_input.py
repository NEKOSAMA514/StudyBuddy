"""只精简模型发送副本；重复 OCR 直接省略，原始识别和依据保留。"""
import re


# 先跳过重复画面，再去除已出现在语音或历史画面的文本行，保留数字和公式差异。
def deduplicate_ocr(payload):
    if "chapters" in payload:
        return {**payload, "chapters": [deduplicate_ocr(chapter) for chapter in payload["chapters"]]}
    if "chapter" in payload:
        return {**payload, "chapter": deduplicate_ocr(payload["chapter"])}
    keys = [key for key in ("evidence", "primary", "context") if key in payload]
    if not keys:
        return payload

    def normalized(text):
        return re.sub(r'[\s，,。；;：:、！？!?“”"‘’]', "", text)

    main = [key for key in keys if key != "context"] or keys
    speech = "".join(item["text"] for key in main for item in payload[key] if item["kind"] == "speech")
    spoken = normalized(speech)
    frames, lines, result = set(), set(), dict(payload)
    for key in keys:
        retained = []
        for item in payload[key]:
            if item["kind"] != "frame":
                retained.append(item)
                continue
            if item["text"] in frames:
                continue
            frames.add(item["text"])
            kept, current = [], set()
            for line in item["text"].splitlines(keepends=True):
                text = line.strip()
                plain = normalized(text)
                # 数字和公式只作原文匹配，避免忽略小数点、正负号等差异。
                literal = any(char.isdigit() for char in text) or re.search(r'[+\-−=<>%\\^_{}\[\]()/]', text)
                repeated_speech = text in speech if literal else bool(plain) and plain in spoken
                if len(plain) >= 6 and (repeated_speech or text in lines):
                    continue
                kept.append(line)
                current.add(text)
            # 同一帧内的相同行仍保留，避免破坏矩阵、表格的重复行。
            lines.update(current)
            text = "".join(kept).strip("\r\n")
            if text.strip():
                retained.append({**item, "text": text})
        result[key] = retained
    if "anchors" in payload and "evidence" in payload:
        retained_ids = {item["id"] for item in result["evidence"]}
        result["anchors"] = [item for item in payload["anchors"] if item["id"] in retained_ids]
    return result


def outline_text_length(payload):
    """统计实际发送的材料与内容类型文字，已省略的重复 OCR 不再计入。"""
    compact = deduplicate_ocr(payload)
    return len(compact.get("content_type", "")) + sum(len(item["text"]) for key in ("evidence", "primary", "context")
               for item in compact.get(key, []))
