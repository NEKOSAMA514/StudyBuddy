from types import SimpleNamespace
import numpy as np
import pytest
from app.config import Settings
from app.recognition.media import Recognizer
from app.recognition.chinese_asr import ChineseASR
from app.errors import AppError
from app.recognition.extraction import extraction_config, extraction_signature
from app.videos.versions import digest


def test_ocr_preserves_lines_and_drops_low_confidence():
    recognizer = Recognizer(Settings(ocr_min_confidence=0.65))
    recognizer.ocr_model = lambda _: SimpleNamespace(txts=("你们每个人身上开两个网口", "噪声", "MAC地址"), scores=(0.99, 0.1, 0.94))
    text, score = recognizer.ocr("unused.jpg")
    assert text == "你们每个人身上开两个网口\nMAC地址"
    assert score == pytest.approx(96.5)
    recognizer.ocr_model = lambda _: SimpleNamespace(txts=None, scores=None)
    assert recognizer.ocr("unused.jpg") == ("", None)


def test_removed_ocr_snapshot_does_not_silently_run_rapidocr():
    recognizer = Recognizer(Settings(ocr_engine="tesseract"))
    recognizer.ocr_model = lambda _: pytest.fail("removed engine must not run RapidOCR")
    with pytest.raises(AppError) as error:
        recognizer.ocr("unused.png")
    assert error.value.code == "unknown_model"


def test_paraformer_uses_original_sample_offsets(monkeypatch):
    samples = np.zeros(48000, dtype=np.float32)
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *args, **kwargs: samples)
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda *args, **kwargs: [{"start": 16000, "end": 32000}])
    received = []
    stream = SimpleNamespace(result=SimpleNamespace(text="网口与交换机"), accept_waveform=lambda rate, piece: received.append((rate, len(piece))))
    recognizer = ChineseASR(Settings())
    recognizer.model = SimpleNamespace(create_stream=lambda: stream, decode_stream=lambda _: None)
    assert recognizer.transcribe("unused.wav") == [{"start_seconds": 1, "end_seconds": 2, "text": "网口与交换机"}]
    assert received == [(16000, 16000)]


def test_corrupt_model_is_rejected_without_inference(tmp_path):
    directory = tmp_path / "paraformer-zh"
    directory.mkdir()
    (directory / "model.int8.onnx").write_bytes(b"corrupt")
    (directory / "tokens.txt").write_text("invalid")
    with pytest.raises(AppError) as error:
        ChineseASR(Settings(model_dir=tmp_path)).load()
    assert error.value.code == "asr_model_invalid"


def test_cache_identity_tracks_engines_and_preserves_old_lookup():
    old = {"extract_version": "extract-v2-word-alignment", "asr_model": "small", "ocr_languages": "chi_sim+eng", "frame_interval": 15}
    assert extraction_signature(old) == digest({"version": old["extract_version"], "asr": "small", "ocr": "chi_sim+eng", "interval": 15})
    current = extraction_config(Settings())
    assert extraction_signature(current) != extraction_signature(old)
    for field, value in [("asr_engine", "whisper"), ("asr_language", "en"), ("ocr_min_confidence", 0.9)]:
        assert extraction_signature({**current, field: value}) != extraction_signature(current)
