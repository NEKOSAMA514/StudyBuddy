from dataclasses import dataclass, field
from pathlib import Path
import os
from .settings.model_defaults import DEFAULT_SUMMARY_PROMPT
from .analysis.budgets import DEFAULT_INPUT_TEXT_LIMIT, OUTPUT_TOKENS
from .settings.model_defaults import MODELSCOPE_PROVIDER, MODELSCOPE_BASE_URL, DEFAULT_LLM_MODEL


# 汇总启动默认值和运行快照，已保存设置在应用装配时覆盖环境初值。
@dataclass
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    log_dir: Path = Path(os.getenv("LOG_DIR", "./log"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    max_duration: float = float(os.getenv("MAX_DURATION_SECONDS", "7200"))
    frame_interval: float = float(os.getenv("FRAME_INTERVAL_SECONDS", "15"))
    asr_model: str = os.getenv("ASR_MODEL", "small")
    asr_engine: str = os.getenv("ASR_ENGINE", "paraformer")
    asr_language: str = os.getenv("ASR_LANGUAGE", "zh")
    asr_threads: int = int(os.getenv("ASR_THREADS", "4"))
    model_dir: Path = Path(os.getenv("MODEL_DIR", "./models"))
    asr_device: str = os.getenv("ASR_DEVICE", "cpu")
    asr_compute: str = os.getenv("ASR_COMPUTE_TYPE", "int8")
    ocr_languages: str = "chi_sim+eng"  # 沿用已保存的缓存签名字段，不作为 RapidOCR 语言选项。
    ocr_engine: str = os.getenv("OCR_ENGINE", "rapidocr")
    ocr_min_confidence: float = float(os.getenv("OCR_MIN_CONFIDENCE", "0.65"))
    llm_provider: str = os.getenv("LLM_PROVIDER", MODELSCOPE_PROVIDER)
    llm_base_url: str = os.getenv("LLM_BASE_URL", MODELSCOPE_BASE_URL)
    llm_model: str = os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL)
    llm_key: str = os.getenv("MODELSCOPE_TOKEN", "") or os.getenv("LLM_API_KEY", "")
    llm_profile_id: str = ""
    llm_profiles: tuple = field(default=(), repr=False)
    llm_stream: bool = os.getenv("LLM_STREAM", "true").lower() == "true"
    llm_json_mode: bool = os.getenv("LLM_JSON_MODE", "false").lower() == "true"
    llm_section_concurrency: int = int(os.getenv("LLM_SECTION_CONCURRENCY", "3"))
    llm_request_interval_seconds: int = 3
    llm_input_text_limit: int = DEFAULT_INPUT_TEXT_LIMIT
    note_style: str = "detailed"
    llm_output_tokens: dict = field(default_factory=lambda: dict(OUTPUT_TOKENS))
    summary_prompt: str = DEFAULT_SUMMARY_PROMPT
    free_tier_confirmed: bool = os.getenv("LLM_FREE_TIER_CONFIRMED", "false").lower() == "true"
    static_dir: Path = Path(os.getenv("STATIC_DIR", "./frontend/dist"))
    video_proxy_enabled: bool = False
    video_proxy_url: str = ""
    mineru_token: str = field(default="", repr=False)

    # 校验运行参数并创建资料根目录，避免非法默认值进入后台流程。
    def prepare(self):
        if self.llm_input_text_limit <= 0:
            raise ValueError("模型输入预算必须为正")
        if self.note_style not in {"concise", "detailed"}:
            raise ValueError("不支持的笔记风格")
        if set(self.llm_output_tokens) != set(OUTPUT_TOKENS) or any(
                type(value) is not int or value <= 0 for value in self.llm_output_tokens.values()):
            raise ValueError("规划输入和各阶段输出上限必须为正整数")
        if not 1 <= self.llm_section_concurrency <= 4:
            raise ValueError("分段模型调用并发数量必须为 1～4")
        if not 1 <= self.llm_request_interval_seconds <= 60:
            raise ValueError("模型请求发送间隔必须为 1～60 秒")
        if self.frame_interval < 1 or self.max_duration <= 0 or self.max_upload_bytes <= 0:
            raise ValueError("文件大小、视频时长和抽帧间隔必须为正，抽帧间隔至少 1 秒")
        if self.asr_engine not in {"paraformer", "whisper"} or self.ocr_engine != "rapidocr":
            raise ValueError("不支持的本地识别引擎")
        if self.asr_engine == "whisper" and self.asr_model not in {"base", "small"}:
            raise ValueError("faster-whisper 仅支持 base 和 small")
        if not 0 <= self.ocr_min_confidence <= 1 or not 1 <= self.asr_threads <= 16:
            raise ValueError("识别置信阈值或线程数量不合法")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "videos").mkdir(exist_ok=True)
