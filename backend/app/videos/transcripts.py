"""保留未经模型改写的识别文本；按提取配置分目录，便于回看和复用。"""
import json
import uuid
from pathlib import Path
from ..util.time import timestamp


def transcript_contents(video_id: str, signature: str, evidence: list[dict], *, filename: str, duration: float, transcript_source=None):
    fields = ("id", "kind", "start_seconds", "end_seconds", "text", "image_name", "confidence")
    items = [{key: item.get(key) for key in fields} for item in sorted(evidence, key=lambda e: (e["start_seconds"], e["end_seconds"], e["id"]))]
    speech = [item for item in items if item["kind"] == "speech"]
    frames = [item for item in items if item["kind"] == "frame"]
    metadata = {"video_title": Path(filename).stem, "source_filename": filename, "duration_seconds": duration}
    subtitle = transcript_source and transcript_source.get("kind") in {"youtube_subtitle", "bilibili_subtitle"}
    source_label = ("平台自动字幕" if transcript_source.get("automatic") else "平台字幕") if subtitle else "语音识别"
    if transcript_source:
        metadata["transcript_source"] = transcript_source
    text = ["原始转录", f"视频标题：{metadata['video_title']}", f"来源文件：{filename}",
            f"视频时长：{timestamp(duration)}", "讲解文本与画面识别分区保存，内容未经总结模型改写。"]
    for heading, group in ((f"一、{source_label}", speech), ("二、画面识别", frames)):
        text.append(f"{heading}\n{'=' * 40}")
        if not group:
            text.append("暂无识别结果。")
        for item in group:
            start, end = timestamp(item["start_seconds"]), timestamp(item["end_seconds"])
            label = f"[{start}–{end}]" if item["kind"] == "speech" else f"[{start}]"
            text.append(f"{label}\n{item['text']}")
    return {"json": json.dumps({"format_version": 2, "video_id": video_id, "extraction_signature": signature,
                                   **metadata, "speech": speech, "frames": frames}, ensure_ascii=False, indent=2),
                "txt": "\n\n".join(text) + "\n"}


# 保存语音和画面识别的结构化与可读副本，查询时只读取现有结果。
def save_transcript(folder: Path, signature: str, evidence: list[dict], *, filename: str, duration: float, transcript_source=None):
    contents = transcript_contents(folder.name, signature, evidence, filename=filename, duration=duration, transcript_source=transcript_source)
    destination = folder / "transcripts" / signature
    destination.mkdir(parents=True, exist_ok=True)
    for extension, content in contents.items():
        path = destination / f"transcript.{extension}"
        temporary = destination / f"{uuid.uuid4().hex}.tmp"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return destination
