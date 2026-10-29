"""文档内嵌图片的本地 OCR，只在遇到图片时加载识别器。"""
import hashlib
import io
import tempfile
from pathlib import Path

from ..errors import AppError, TaskInterrupted

MAX_IMAGES = 200
MAX_IMAGE_PIXELS = 20_000_000


class ImageText:
    def __init__(self, settings=None, diagnostics=None, check=lambda: None):
        self.settings = settings
        self.diagnostics = diagnostics
        self.recognizer = None
        self.cache = {}
        self.warnings = []
        self.check = check

    # 以图片字节摘要复用 OCR 结果，并把识别失败记录为来源明确的警告。
    def read(self, raw, label, *, cache_key=None):
        self.check()
        key = cache_key if cache_key is not None else hashlib.sha256(raw).hexdigest()
        if key not in self.cache:
            if len(self.cache) >= MAX_IMAGES:
                raise AppError("document_limit", "文档超过200张不同的内嵌图片，请拆分后上传。")
            self.cache[key] = self.recognize(raw)
        text, problem = self.cache[key]
        if problem:
            message = f"{label}：{problem}"
            if message not in self.warnings:
                self.warnings.append(message)
            return ""
        return "图片文字（本地 OCR，识别可能有误）：\n" + text if text.strip() else ""

    # 规范图片方向与透明背景后按需识别，取消信号和资源限制继续向上抛出。
    def recognize(self, raw):
        self.check()
        if self.settings is None:
            return "", "未启用本地 OCR，不能据此判断原图是否包含文字。"
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise AppError("document_limit", "文档单张图片超过2000万像素，请缩小图片后上传。")
                image = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                # 幻灯片中的小型终端截图，字符高度往往只有 10–15 像素。
                background = ImageOps.autocontrast(ImageOps.grayscale(background))
                scale = min(4, 2400 / max(background.size))
                if scale > 1:
                    background = background.resize(tuple(round(v * scale) for v in background.size), Image.Resampling.LANCZOS)
                if self.recognizer is None:
                    from ..recognition.media import Recognizer
                    self.recognizer = Recognizer(self.settings)
                with tempfile.TemporaryDirectory(prefix="studybuddy-document-ocr-") as folder:
                    path = Path(folder) / "image.png"
                    background.save(path)
                    text, _confidence = self.recognizer.ocr(path, preserve_rows=True)
                    self.check()
            if text.strip():
                return text.strip(), ""
            return "", "未识别到文字，可能为非文字图片或识别遗漏。"
        except (AppError, TaskInterrupted):
            raise
        except Exception as exc:
            problem = "本地 OCR 读取失败，不能据此判断原图是否包含文字。"
            if self.diagnostics is not None:
                reference = self.diagnostics.record("document_ocr_failed", problem, exc, ocr_engine=self.settings.ocr_engine)
                problem += f" 错误编号：{reference['error_id']}。"
                if reference.get("log_file"):
                    problem += f" 详细日志：{reference['log_file']}。"
                elif reference.get("log_saved") is False:
                    problem += " 详细日志写入失败，请检查日志目录权限和磁盘空间。"
            return "", problem
