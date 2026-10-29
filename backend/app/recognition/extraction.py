"""识别配置、复用条件及已有缓存路径；不依赖生成流程。"""
import hashlib
import json
import math

EXTRACT_VERSION = "extract-v3-chinese-region-ocr"
RECOGNIZER_FIELDS = ("asr_engine", "asr_model", "asr_language", "asr_device", "asr_compute", "asr_threads",
                     "ocr_engine", "ocr_languages", "ocr_min_confidence", "model_dir")


# 仅用影响识别器实例的配置组成复用键，模型生成设置不触发重建。
def recognizer_key(settings):
    return tuple(getattr(settings, field) for field in RECOGNIZER_FIELDS)


def extraction_config(settings, previous=None):
    config = {"asr_model": settings.asr_model, "ocr_languages": settings.ocr_languages,
            "frame_interval": settings.frame_interval, "extract_version": EXTRACT_VERSION,
            "frame_count": 60,
            "asr_engine": settings.asr_engine, "asr_language": settings.asr_language,
            "ocr_engine": settings.ocr_engine, "ocr_min_confidence": settings.ocr_min_confidence}
    if previous is not None:
        config["extract_version"] = previous.get("extract_version", EXTRACT_VERSION)
        if "frame_count" in previous:
            config["frame_count"] = previous["frame_count"]
        else:
            config.pop("frame_count")
    return config


# 新任务使用均匀采样点，历史任务仍按保存的固定间隔还原。
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
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def speech_signature(config):
    # ASR 不受截图数量和字幕来源影响，继续复用原来的语音缓存。
    return extraction_signature({key: value for key, value in config.items()
                                 if key not in {"frame_count", "transcript_source"}})


def speech_cache_path(folder, signature):
    return folder / f"speech-{signature}.json"


def ocr_cache_path(folder, signature, index):
    return folder / f"ocr-{signature}-{index:05d}.json"
