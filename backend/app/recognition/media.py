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

    def ocr(self, image: Path, *, preserve_rows=False):
        self.check_stop()
        if self.settings.ocr_engine != "rapidocr":
            raise AppError("unknown_model", "不支持该 OCR 模型，请选择 RapidOCR。")
        from rapidocr import RapidOCR, OCRVersion, ModelType
        if self.ocr_model is None:
            from .model_catalog import model_path
            from .model_files import prepare_bundled
            prepare_bundled(self.settings, "rapidocr")
            directory = model_path(self.settings, "rapidocr")
            self.ocr_model = RapidOCR(params={"Global.log_level": "warning",
                "Det.model_path": str(directory / "PP-OCRv6_det_small.onnx"),
                "Rec.model_path": str(directory / "PP-OCRv6_rec_small.onnx"),
                "Cls.model_path": str(directory / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
                "EngineConfig.onnxruntime.intra_op_num_threads": self.settings.asr_threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Det.limit_side_len": 1280, "Global.text_score": self.settings.ocr_min_confidence,
                "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.SMALL})
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
