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


def test_video_ocr_filters_entire_lines_at_inclusive_confidence_cutoff():
    from io import BytesIO
    from time import monotonic
    import zlib
    from app.recognition.video_ocr import Candidate, evidence_rows, read_crops

    shape = (4, 12, 3)
    crop = zlib.compress(bytes(int(np.prod(shape))))
    crop_size = len(crop)
    crops = BytesIO(crop * 3)
    candidates = [
        Candidate(1, 3, (0, 1, 12, 5), 1, 3, 0, crop_size, shape),
        Candidate(1, 8, (0, 6, 12, 10), 1, 8, crop_size, crop_size, shape),
        Candidate(1, 2, (0, 11, 12, 15), 1, 2, crop_size * 2, crop_size, shape),
    ]
    engine = SimpleNamespace(
        _load_rec_model=lambda: SimpleNamespace(rec_image_shape=(3, 48, 320), rec_batch_num=6),
        cls_and_rotate=lambda images: (images, None),
        recognize_txt=lambda images: SimpleNamespace(txts=("整行保留", "整行删除", "后一行"),
                                                     scores=(.90, .89999, .96)),
    )
    accepted, used = read_crops(engine, crops, candidates, monotonic() + 5, lambda: None)
    assert used == 3
    assert evidence_rows(accepted) == [
        {"start_seconds": 1, "end_seconds": 3, "text": "整行保留\n后一行", "confidence": pytest.approx(93)},
    ]


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
    for field, value in [("asr_engine", "whisper"), ("asr_language", "en"), ("ocr_min_confidence", 0.91)]:
        assert extraction_signature({**current, field: value}) != extraction_signature(current)
