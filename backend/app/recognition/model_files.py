"""下载与识别共用的内置权重准备及 Paraformer 文件校验。"""
import hashlib
from pathlib import Path
import shutil
import uuid
from ..errors import AppError
from .model_catalog import MODELS, model_path


MODEL_SHA256 = "f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945"


def verify_paraformer(directory):
    with (directory / "model.int8.onnx").open("rb") as source:
        valid_model = hashlib.file_digest(source, "sha256").hexdigest() == MODEL_SHA256
    tokens = (directory / "tokens.txt").read_bytes()
    valid_tokens = hashlib.sha1(f"blob {len(tokens)}\0".encode() + tokens).hexdigest() == "57bc045ddda0434ed4440c38e14287c595b258d9"
    if not valid_model or not valid_tokens:
        raise AppError("asr_model_invalid", "本地中文模型文件校验失败，请重新下载 Paraformer 模型。")


# 按需复制随包 OCR 权重，已有文件默认复用，强制准备时重新复制。
def prepare_bundled(settings, identifier, force=False):
    directory = model_path(settings, identifier)
    directory.mkdir(parents=True, exist_ok=True)
    import rapidocr
    source = Path(rapidocr.__file__).parent / "models"
    for file in MODELS[identifier]["files"]:
        if force or not (directory / file).is_file():
            temporary = directory / f".{file}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(source / file, temporary)
                temporary.replace(directory / file)
            finally:
                temporary.unlink(missing_ok=True)
