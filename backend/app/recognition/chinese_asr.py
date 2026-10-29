"""本地中文 Paraformer；音频切分的采样位置直接产生时间证据。"""
from pathlib import Path
from .model_files import verify_paraformer


# 封装 Paraformer 的按需加载与中文转写，将模型输出转换为统一片段。
class ChineseASR:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.check_stop = lambda: None

    def load(self):
        import sherpa_onnx
        from huggingface_hub import hf_hub_download
        from .model_catalog import MODELS, model_path
        spec = MODELS["paraformer"]
        directory = model_path(self.settings, "paraformer")
        directory.mkdir(parents=True, exist_ok=True)
        files = {}
        for filename in spec["files"]:
            local = directory / filename
            files[filename] = str(local) if local.is_file() else hf_hub_download(spec["repo"], filename, revision=spec["revision"], local_dir=directory)
        verify_paraformer(directory)
        self.model = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=files["model.int8.onnx"], tokens=files["tokens.txt"],
            num_threads=self.settings.asr_threads, sample_rate=16000, feature_dim=80,
            decoding_method="greedy_search", provider="cpu")

    def transcribe(self, audio: Path):
        self.check_stop()
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        if self.model is None:
            self.load()
        samples = decode_audio(str(audio), sampling_rate=16000)
        ranges = get_speech_timestamps(samples, VadOptions(min_silence_duration_ms=400,
            speech_pad_ms=150, max_speech_duration_s=12))
        output = []
        for item in ranges:
            self.check_stop()
            start, end = item["start"], item["end"]
            stream = self.model.create_stream()
            stream.accept_waveform(16000, samples[start:end])
            self.model.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                output.append({"start_seconds": start / 16000, "end_seconds": end / 16000, "text": text})
        return output
