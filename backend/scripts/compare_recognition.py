"""真实文件离线对比，绝不调用总结 API。报告包含原始识别输出而非虚构准确率。"""
import argparse
import json
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.recognition.media import Recognizer
from app.videos.video_reader import extract_frame
from app.analysis.checkpoints import atomic_json
from app.recognition.extraction import extraction_config, extraction_signature


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    settings = Settings()
    recognizer = Recognizer(settings)
    folder = args.folder
    original = next(folder.glob("original.*"), folder / "original.mp4")
    report = {"config": extraction_config(settings), "cloud_model_tested": False}
    started = time.monotonic()
    speech = recognizer.transcribe(folder / "audio.wav")
    report["asr_seconds"] = round(time.monotonic() - started, 3)
    report["speech"] = speech
    # 后续正式任务按相同版本与配置校验缓存；不用重新识别已成功的语音。
    signature = extraction_signature(extraction_config(settings))
    atomic_json(folder / f"speech-{signature}.json", speech)
    report["ocr"] = []
    started = time.monotonic()
    for seconds in [15, 30, 60, 120, 300, 600]:
        image = folder / f"quality-check-{seconds}.jpg"
        extract_frame(original, image, seconds)
        text, confidence = recognizer.ocr(image)
        report["ocr"].append({"seconds": seconds, "text": text, "confidence": confidence})
    report["ocr_seconds"] = round(time.monotonic() - started, 3)
    output = settings.data_dir / "validation" / "recognition-v3-comparison.json"
    output.parent.mkdir(exist_ok=True)
    atomic_json(output, report)
    print(json.dumps({"asr_seconds": report["asr_seconds"], "speech_segments": len(speech),
                      "ocr_seconds": report["ocr_seconds"], "speech_preview": speech[:4], "ocr": report["ocr"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
