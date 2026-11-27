"""识别配置、复用条件及已有缓存路径；不依赖生成流程。"""
from dataclasses import dataclass
import hashlib
import json
import math

LEGACY_EXTRACT_VERSION = "extract-v3-chinese-region-ocr"
PRIORITY_ONLY_EXTRACT_VERSION = "extract-v13-video-line-ocr"
BURST_EXTRACT_VERSION = "extract-v14-video-line-ocr"
SPOKEN_CAPTION_EXTRACT_VERSION = "extract-v15-video-line-ocr"
DENSE_SCREEN_EXTRACT_VERSION = "extract-v16-video-line-ocr"
EXTRACT_VERSION = DENSE_SCREEN_EXTRACT_VERSION
VIDEO_OCR_INTERVAL = 1
VIDEO_OCR_CONFIDENCE = 0.90
LINE_OCR_IMAGE_PREFIX = "ocr-line-"
RECOGNIZER_FIELDS = ("asr_engine", "asr_model", "asr_language", "asr_device", "asr_compute", "asr_threads",
                     "ocr_engine", "ocr_languages", "ocr_min_confidence", "model_dir")


@dataclass(frozen=True)
class VideoOcrPolicy:
    reduce_middle_versions: bool = False
    reduce_column_refreshes: bool = False
    reduce_dense_screens: bool = False
    use_speech: bool = False


# 历史任务缺少缓存时，按保存的提取版本重建原有画面证据。
_VIDEO_OCR_POLICIES = {
    **{f"extract-v{version}-video-line-ocr": VideoOcrPolicy() for version in range(4, 13)},
    PRIORITY_ONLY_EXTRACT_VERSION: VideoOcrPolicy(reduce_middle_versions=True),
    BURST_EXTRACT_VERSION: VideoOcrPolicy(reduce_middle_versions=True, reduce_column_refreshes=True),
    SPOKEN_CAPTION_EXTRACT_VERSION: VideoOcrPolicy(reduce_middle_versions=True, reduce_column_refreshes=True,
                                                  use_speech=True),
    DENSE_SCREEN_EXTRACT_VERSION: VideoOcrPolicy(reduce_middle_versions=True, reduce_column_refreshes=True,
                                                reduce_dense_screens=True, use_speech=True),
}


def video_ocr_policy(extract_version):
    return _VIDEO_OCR_POLICIES[extract_version]


# 仅用影响识别器实例的配置组成复用键，模型生成设置不触发重建。
def recognizer_key(settings):
    return tuple(getattr(settings, field) for field in RECOGNIZER_FIELDS)


def extraction_config(settings, previous=None):
    config = {"asr_model": settings.asr_model, "ocr_languages": settings.ocr_languages,
            "frame_interval": settings.frame_interval, "extract_version": LEGACY_EXTRACT_VERSION,
            "asr_engine": settings.asr_engine, "asr_language": settings.asr_language,
            "ocr_engine": settings.ocr_engine, "ocr_min_confidence": settings.ocr_min_confidence}
    if previous is None:
        return {**config, "speech_cache_signature": speech_signature(config),
                "extract_version": EXTRACT_VERSION, "frame_interval": VIDEO_OCR_INTERVAL,
                "ocr_min_confidence": VIDEO_OCR_CONFIDENCE}
    config.update({key: previous[key] for key in ("extract_version", "frame_count", "speech_cache_signature")
                   if key in previous})
    return config


def uses_line_ocr(config):
    return config.get("extract_version") in _VIDEO_OCR_POLICIES


def uses_speech_for_ocr(config):
    policy = _VIDEO_OCR_POLICIES.get(config.get("extract_version"))
    return policy is not None and policy.use_speech


# 还原历史任务的均匀采样位置或固定间隔位置。
def frame_times(config, duration):
    if "frame_count" in config:
        return [i * (duration / config["frame_count"]) for i in range(config["frame_count"])]
    return [i * config["frame_interval"] for i in range(math.ceil(duration / config["frame_interval"]))]


# 用识别参数及采样版本生成缓存身份，避免不同规则误用同一结果。
def extraction_signature(config):
    value = {"version": config["extract_version"], "asr": config["asr_model"], "ocr": config["ocr_languages"]}
    if "frame_count" in config:
        value["frame_count"] = config["frame_count"]
    else:
        value["interval"] = config["frame_interval"]
    if config.get("asr_engine"):
        value.update({key: config[key] for key in ("asr_engine", "asr_language", "ocr_engine", "ocr_min_confidence")})
    if config.get("transcript_source", {}).get("kind") in {"youtube_subtitle", "bilibili_subtitle"}:
        value["transcript_source"] = config["transcript_source"]
    if config.get("ocr_speech_digest") and uses_speech_for_ocr(config):
        value["ocr_speech_digest"] = config["ocr_speech_digest"]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def ocr_cache_signature(config):
    # 转录内容参与 OCR 时影响画面缓存键，字幕来源元数据用于区分完整依据。
    return extraction_signature({key: value for key, value in config.items() if key != "transcript_source"})


def speech_signature(config):
    # ASR 不受截图数量和字幕来源影响，继续复用原来的语音缓存。
    if "speech_cache_signature" in config:
        return config["speech_cache_signature"]
    return extraction_signature({key: value for key, value in config.items()
                                 if key not in {"frame_count", "transcript_source"}})


def speech_cache_path(folder, signature):
    return folder / f"speech-{signature}.json"


def ocr_cache_path(folder, signature, index):
    return folder / f"ocr-{signature}-{index:05d}.json"


def line_ocr_cache_path(folder, signature):
    return folder / f"ocr-{signature}-lines.json"


def line_ocr_evidence(rows, video_id, signature, recognition_signature):
    return [{**{key: value for key, value in row.items() if key != "clipped_lines"},
             "id": f"{video_id}-{signature}-frame-{index}", "kind": "frame",
             "image_name": f"{LINE_OCR_IMAGE_PREFIX}{recognition_signature}-{index:05d}.jpg"}
            for index, row in enumerate(rows)]
