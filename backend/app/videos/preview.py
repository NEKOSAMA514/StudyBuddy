"""仅从已有识别缓存只读估算，不执行识别或请求模型。"""
import json
import math
from pathlib import Path
from ..errors import AppError
from ..recognition.extraction import extraction_signature, speech_signature, speech_cache_path, ocr_cache_path, frame_times
from ..video_download.subtitles import SUBTITLE_KINDS, subtitle_cache, content_digest
from .model import ModelClient
from .segments import split_segments, outline_measure
from .concise import plan


def analysis_preview(video, folder, config, settings):
    pending = {"character_count": None, "estimated_segments": None,
               "reason": "识别材料尚不完整，识别完成后可查看字符数与预计段数。"}
    try:
        source = config.get("transcript_source", {})
        if source.get("kind") in SUBTITLE_KINDS:
            speech = json.loads(subtitle_cache(folder, source).read_text("utf-8"))
            if content_digest(speech) != source["digest"]:
                return pending
        elif video.audio_codec:
            speech = json.loads(speech_cache_path(folder, speech_signature(config)).read_text("utf-8"))
        else:
            speech = []
        signature = extraction_signature(config)
        evidence = []
        for index, item in enumerate(speech):
            start, end = float(item["start_seconds"]), float(item["end_seconds"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or start >= video.duration:
                continue
            if item["text"].strip():
                evidence.append({"id": f"{video.id}-{signature}-speech-{index}", "kind": "speech",
                    "start_seconds": start, "end_seconds": min(end, video.duration), "text": item["text"],
                    "image_name": None, "confidence": None})
        recognition_signature = extraction_signature({key: value for key, value in config.items() if key != "transcript_source"})
        for index, _ in enumerate(frame_times(config, video.duration)):
            item = json.loads(ocr_cache_path(folder, recognition_signature, index).read_text("utf-8"))
            if item["text"].strip():
                evidence.append({**item, "id": f"{video.id}-{signature}-frame-{index}"})
    except (OSError, ValueError, KeyError, TypeError):
        return pending
    evidence.sort(key=lambda e: (e["start_seconds"], e["end_seconds"], e["id"]))
    count = sum(len(item["text"]) for item in evidence)
    if not evidence:
        return {"character_count": 0, "estimated_segments": 0, "reason": "未识别到有效文字，本次处理将重新检查材料。"}
    model = ModelClient(settings)
    title = Path(video.filename).stem
    try:
        if settings.note_style == "concise":
            segments = plan(evidence, model, settings.llm_input_text_limit, title)
        else:
            # 详尽生成先分配紧凑的 E 编号，再计量大纲请求的输入。
            items = [{"id": f"E{index + 1}", **{key: item[key] for key in ("kind", "start_seconds", "end_seconds", "text")}}
                     for index, item in enumerate(evidence)]
            measure = outline_measure(model, title)
            if measure({"evidence": [], "context": []}) >= settings.llm_input_text_limit:
                raise AppError("model_input_budget", "提示词与补充要求未留下材料空间，请提高生成输入上限。")
            _, segments = split_segments(items, video.duration, settings.llm_input_text_limit, measure)
        return {"character_count": count, "estimated_segments": len(segments), "reason": None}
    except AppError as error:
        if error.code != "model_input_budget":
            raise
        return {"character_count": count, "estimated_segments": None, "reason": error.message}
