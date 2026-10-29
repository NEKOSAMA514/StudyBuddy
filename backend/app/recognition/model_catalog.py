"""共用的识别模型目录、路径及就绪查询；不加载识别运行库。"""
import importlib.util


MODELS = {
    "paraformer": {"kind": "asr", "name": "Paraformer · 中文轻量（默认）", "engine": "paraformer", "model": "small", "directory": "paraformer-zh", "description": "适用于中文（普通话）。中文优先，侧重速度和较低资源占用。", "repo": "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14", "revision": "def027084691107096b5ebba69785756d63de6c5", "files": ["model.int8.onnx", "tokens.txt"], "size": "约 243 MB", "docs": "https://k2-fsa.github.io/sherpa/onnx/paraformer/index.html"},
    **{f"whisper-{size}": {"kind": "asr", "name": f"faster-whisper · {size}", "engine": "whisper", "model": size, "directory": f"faster-whisper-{size}", "description": f"适用于中文、英文等多种语言。{position}", "repo": f"Systran/faster-whisper-{size}", "files": ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"], "size": amount, "docs": "https://github.com/SYSTRAN/faster-whisper"} for size, amount, position in [("base", "约 145 MB", "速度与准确率折中，资源占用较低。"), ("small", "约 486 MB", "更重视准确率，耗时更长、资源占用更高。")]},
    "rapidocr": {"kind": "ocr", "name": "RapidOCR · PP-OCRv6 small（默认）", "directory": "rapidocr-v6", "description": "适用于中文、英文，适合清晰 PPT 和视频截图。", "files": ["PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"], "size": "约 32 MB", "docs": "https://github.com/RapidAI/RapidOCR"},
}


def option_for(settings):
    if settings.asr_engine == "whisper":
        return f"whisper-{settings.asr_model}"
    return settings.asr_engine


def model_path(settings, identifier):
    return settings.model_dir / MODELS[identifier]["directory"]


# 检查所选型号的本地文件是否完整，和运行库是否可用分别判断。
def model_ready(settings, identifier):
    path = model_path(settings, identifier)
    return all((path / file).is_file() and (path / file).stat().st_size > 0 for file in MODELS[identifier]["files"])


# 检查识别后端所需组件能否加载，不把文件存在等同于可运行。
def runtime_ready(identifier):
    if identifier not in MODELS:
        return False
    package = "faster_whisper" if identifier.startswith("whisper-") else "rapidocr" if identifier == "rapidocr" else "sherpa_onnx"
    return importlib.util.find_spec(package) is not None
