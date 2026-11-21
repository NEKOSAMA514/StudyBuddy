from pathlib import Path
from ..errors import AppError


# 依据文字框位置合并同一行，保留代码和表格所需的行结构。
def text_rows(boxes, texts):
    rows = []
    for box, text in sorted(zip(boxes, texts), key=lambda item: min(float(p[1]) for p in item[0])):
        left, right = min(float(p[0]) for p in box), max(float(p[0]) for p in box)
        top, bottom = min(float(p[1]) for p in box), max(float(p[1]) for p in box)
        height = max(1, bottom - top)
        for row in reversed(rows):
            overlap = min(bottom, row["bottom"]) - max(top, row["top"])
            if overlap >= 0.5 * min(height, row["bottom"] - row["top"]) and abs((top + bottom) - (row["top"] + row["bottom"])) < height:
                row["parts"].append((left, right, text))
                break
        else:
            rows.append({"top": top, "bottom": bottom, "parts": [(left, right, text)]})
    return "\n".join(" ".join(part[2] for part in sorted(row["parts"])) for row in rows)


# 按配置按需加载本地 ASR 与 OCR，统一输出时间片段和置信信息。
class Recognizer:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.chinese = None
        self.ocr_model = None
        self.video_ocr_model = None
        self.cancel = None
        self.check_stop = lambda: None

    def transcribe(self, audio: Path):
        self.check_stop()
        if self.settings.asr_engine == "paraformer":
            from .chinese_asr import ChineseASR
            if self.chinese is None:
                self.chinese = ChineseASR(self.settings)
            self.chinese.check_stop = self.check_stop
            return self.chinese.transcribe(audio)
        if self.settings.asr_engine != "whisper":
            raise AppError("unknown_model", "请选择列表中的语音识别模型。")
        from faster_whisper import WhisperModel
        if self.model is None:
            from .model_catalog import MODELS, option_for, model_path, model_ready
            identifier = option_for(self.settings)
            if identifier not in MODELS:
                raise AppError("unknown_model", "请选择列表中的语音识别模型。")
            if not model_ready(self.settings, identifier):
                raise AppError("model_not_downloaded", "请在模型设置中先下载 faster-whisper 模型。")
            self.model = WhisperModel(str(model_path(self.settings, identifier)), device=self.settings.asr_device,
                                      compute_type=self.settings.asr_compute, local_files_only=True)
        segments, _ = self.model.transcribe(str(audio), beam_size=5, vad_filter=True, word_timestamps=True,
            language=None if self.settings.asr_language == "auto" else self.settings.asr_language,
            condition_on_previous_text=False)
        output = []
        for segment in segments:
            self.check_stop()
            if not segment.text.strip():
                continue
            # 用首尾词的对齐位置收紧片段，减少点击后落在长静音里的情况。
            # 这是模型对齐，仍然是近似值，不是人工校正的精确字幕。
            words = segment.words or []
            output.append({"start_seconds": words[0].start if words else segment.start,
                           "end_seconds": words[-1].end if words else segment.end,
                           "text": segment.text.strip()})
        return output

    def _load_ocr_model(self, *, video=False):
        if self.settings.ocr_engine != "rapidocr":
            raise AppError("unknown_model", "不支持该 OCR 模型，请选择 RapidOCR。")
        from rapidocr import ModelType, OCRVersion, RapidOCR
        from .model_catalog import ready_ocr_path
        directory = ready_ocr_path(self.settings)
        if directory is None:
            raise AppError("model_not_downloaded", "OCR 模型文件不完整，请检查运行环境。")
        detector = (Path(__file__).with_name("weights") / "PP-OCRv6_det_tiny.onnx" if video
                    else directory / "PP-OCRv6_det_small.onnx")
        paths = {"Det": str(detector), "Rec": str(directory / "PP-OCRv6_rec_small.onnx"),
                 "Cls": str(directory / "ch_ppocr_mobile_v2.0_cls_mobile.onnx")}
        engine = RapidOCR(params={"Global.log_level": "warning",
            **{f"{key}.model_path": path for key, path in paths.items()},
            "EngineConfig.onnxruntime.intra_op_num_threads": self.settings.asr_threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.limit_side_len": 640 if video else 1280, "Global.text_score": self.settings.ocr_min_confidence,
            **({"Det.limit_type": "max"} if video else {}),
            "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.TINY if video else ModelType.SMALL,
            "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.SMALL})
        if video:
            # 找框和读字分阶段执行，空闲会话无需占用 CPU 等待。
            from omegaconf import flag_override
            from onnxruntime import InferenceSession, SessionOptions
            options = SessionOptions()
            options.intra_op_num_threads = self.settings.asr_threads
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "0")
            for key, path in paths.items():
                with flag_override(engine.cfg[key], "allow_objects", True):
                    engine.cfg[key].session = InferenceSession(path, sess_options=options,
                                                               providers=["CPUExecutionProvider"])
        return engine

    def ocr_video(self, samples, on_sample, *, reduce_middle_versions=True, reduce_column_refreshes=True):
        from .video_ocr import recognize_lines
        self.check_stop()
        return recognize_lines(samples, self._video_engine, self.check_stop, on_sample,
                               reduce_middle_versions=reduce_middle_versions, reduce_column_refreshes=reduce_column_refreshes)

    def _video_engine(self):
        if self.video_ocr_model is None:
            self.video_ocr_model = self._load_ocr_model(video=True)
        return self.video_ocr_model

    def ocr(self, image: Path, *, preserve_rows=False):
        self.check_stop()
        if self.settings.ocr_engine != "rapidocr":
            raise AppError("unknown_model", "不支持该 OCR 模型，请选择 RapidOCR。")
        if self.ocr_model is None:
            self.ocr_model = self._load_ocr_model()
        result = self.ocr_model(str(image))
        # 检测每个文字区域后识别；保留行结构，不把汉字强行用空格隔开。
        lines = [(str(text).strip(), float(score)) for text, score in zip(result.txts or [], result.scores or [])
                 if str(text).strip() and float(score) >= self.settings.ocr_min_confidence]
        text = "\n".join(text for text, _ in lines)
        if preserve_rows and lines:
            selected = [(box, str(text).strip()) for box, text, score in zip(result.boxes, result.txts, result.scores)
                        if str(text).strip() and float(score) >= self.settings.ocr_min_confidence]
            text = text_rows([item[0] for item in selected], [item[1] for item in selected])
        return text, (100 * sum(score for _, score in lines) / len(lines) if lines else None)
