from types import SimpleNamespace
import numpy as np
import pytest
from app.config import Settings
from app.recognition.media import Recognizer
from app.recognition.chinese_asr import ChineseASR
from app.errors import AppError
from app.recognition.extraction import extraction_config, extraction_signature, speech_signature, uses_line_ocr
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
        cls_and_rotate=lambda images: (images, None),
        recognize_txt=lambda images: SimpleNamespace(txts=("整行保留", "整行删除", "后一行"),
                                                     scores=(.90, .89999, .96)),
    )
    accepted, used = read_crops(engine, crops, candidates, lambda: None)
    assert used == 3
    assert evidence_rows(accepted) == [
        {"start_seconds": 1, "end_seconds": 2, "text": "后一行", "confidence": pytest.approx(96)},
        {"start_seconds": 1, "end_seconds": 3, "text": "整行保留", "confidence": pytest.approx(90)},
    ]


def test_video_ocr_tries_alternate_crop_only_for_low_confidence():
    from io import BytesIO
    import zlib
    from app.recognition.video_ocr import Candidate, evidence_rows, read_crops

    shape = (4, 12, 3)
    crop = zlib.compress(bytes(int(np.prod(shape))))
    alternate = Candidate(4, 6, (0, 0, 12, 4), 1, 3, len(crop), len(crop), shape)
    primary = Candidate(4, 6, (0, 0, 12, 4), 2, 3, 0, len(crop), shape, alternate)
    for first_score, expected in ((.87, 2), (.99, 1)):
        scores = iter((first_score, .94))
        engine = SimpleNamespace(
            cls_and_rotate=lambda images: (images, None),
            recognize_txt=lambda _: SimpleNamespace(txts=("同一行",), scores=(next(scores),)),
        )
        accepted, used = read_crops(engine, BytesIO(crop * 2), [primary], lambda: None)
        assert used == expected
        assert evidence_rows(accepted) == [
            {"start_seconds": 4, "end_seconds": 6, "text": "同一行",
             "confidence": pytest.approx(94 if expected == 2 else 99)},
        ]


@pytest.mark.parametrize("case", ["clipped", "other_row", "one_character", "missing_negation", "split_formula", "reappears"])
def test_video_ocr_requires_two_matching_full_lines_to_fold_a_crop(case):
    from app.recognition.video_ocr import Candidate, evidence_rows

    whole = "滴定开始前加入1-3mL试剂，慢慢搅拌并观察沉淀"
    part = "3mL试剂，慢慢搅拌并观察沉淀" if case == "split_formula" else "慢慢搅拌并观察沉淀"
    if case == "missing_negation":
        whole, part = "不需要搅拌即可观察溶液颜色", "需要搅拌即可观察溶液颜色"
    width = 10 * len(whole)
    offset = 10 * whole.index(part)
    top = 40 if case == "other_row" else 10
    if case == "one_character":
        part = part.replace("沉淀", "沉积")

    def read(seconds, box, text):
        return Candidate(seconds, seconds, box, 1, 1, 0, 0, ()), text, .95

    rows = evidence_rows([
        read(0, (10, 10, 10 + width, 30), whole),
        read(1, (10 + offset, top, 10 + width, top + 20), part),
        read(8 if case == "reappears" else 3, (10, 10, 10 + width, 30), whole),
    ])
    assert [row["text"] for row in rows] == [whole, part, whole]
    assert rows[1].get("clipped_lines") == ([[0, 0, 2]] if case == "clipped" else None)


def test_video_ocr_reads_all_tracks_after_a_slow_recognition_batch(monkeypatch):
    import zlib
    from app.recognition import video_ocr

    shape = (4, 12, 3)
    crop = zlib.compress(bytes(int(np.prod(shape))))
    count = video_ocr.BATCH_SIZE + 1
    batches = []
    seconds = 0

    def tracker(_, crops):
        crops.write(crop)
        candidates = [video_ocr.Candidate(i, i, (0, 0, 12, 4), 1, 1, 0, len(crop), shape) for i in range(count)]
        return SimpleNamespace(active=[], candidates=candidates, finish=lambda _: None, observe=lambda *_: None)

    def read(images):
        nonlocal seconds
        batches.append(len(images))
        seconds += 120
        return SimpleNamespace(txts=("整行文字",) * len(images), scores=(.99,) * len(images))

    monkeypatch.setattr(video_ocr, "TextRegions", tracker)
    monkeypatch.setattr(video_ocr, "monotonic", lambda: seconds)
    engine = SimpleNamespace(
        _load_det_model=lambda: None,
        cls_and_rotate=lambda images: (images, None),
        recognize_txt=read,
    )
    rows = video_ocr.recognize_lines([(0, None)], lambda: engine, lambda: None, lambda _: None)
    assert batches == [video_ocr.BATCH_SIZE, 1]
    assert [row["start_seconds"] for row in rows] == list(range(count))


def test_video_ocr_ranks_a_track_across_its_windows_once():
    from app.recognition.video_ocr import Candidate, rank

    box, shape = (0, 0, 12, 4), (4, 12, 3)
    long = Candidate(0, 48, box, 1, 49, 0, 12, shape)
    early = Candidate(0, 5, box, 10, 6, 12, 12, shape)
    late = Candidate(49, 50, box, 10, 2, 24, 12, shape)
    assert list(rank([early, long, late])) == [long, early, late]


@pytest.mark.parametrize("case", ["jitter", "partial", "full_fragment", "occluded_fragment", "changed_fragment",
                                 "different_height", "digit_changed", "extended", "disappeared"])
def test_video_ocr_keeps_unchanged_lines_during_detection(case):
    from io import BytesIO
    from app.recognition.video_ocr import TextRegions

    def polygon(left, top, right, bottom):
        return np.array([[left, top], [right, top], [right, bottom], [left, bottom]], dtype=np.float32)

    title = polygon(18, 12, 167, 29)
    frame = np.zeros((64, 224, 3), dtype=np.uint8)
    frame[13:28, 19:166:3] = 255
    full = (0, 0, 224, 64)
    partial = (104, 16, 200, 48)
    scans = []
    boxes = [title]

    def detect(image):
        scans.append(image.shape[:2])
        return SimpleNamespace(boxes=np.array(boxes) if boxes else None)

    tracker = TextRegions(detect, BytesIO())

    def refresh(seconds, region):
        gray = frame[:, :, 0].copy()
        tracker.reference = gray.copy()
        tracker.refresh(SimpleNamespace(to_ndarray=lambda **_: frame), 0, seconds, gray, [region])

    refresh(0, full)
    region = full
    if case == "jitter":
        boxes = [polygon(15, 12, 167, 29)]
    elif case == "partial":
        region = partial
        boxes = [polygon(0, 0, 62, 13)]
    elif case in {"full_fragment", "occluded_fragment", "changed_fragment"}:
        boxes = [polygon(104, 12, 166, 29)]
        if case == "occluded_fragment":
            frame[12:30, :104] = 0
        elif case == "changed_fragment":
            frame[13:28, 117:128] = 255
    elif case == "different_height":
        boxes = [polygon(104, 21, 166, 29)]
    elif case == "digit_changed":
        frame[13:28, 90:101] = 255
    elif case == "extended":
        frame[13:28, 171:200:3] = 255
        boxes = [polygon(18, 12, 200, 29)]
    elif case == "disappeared":
        frame[:] = 0
        region, boxes = partial, []
    refresh(1, region)

    active = [(track.first, track.last) for track in tracker.active]
    finished = [(candidate.first, candidate.last) for candidate in tracker.candidates]
    if case in {"jitter", "partial", "full_fragment"}:
        assert active == [(0, 1)] and finished == []
    elif case == "different_height":
        assert active == [(1, 1), (0, 1)] and finished == []
    else:
        assert active == ([] if case == "disappeared" else [(1, 1)])
        assert finished == [(0, 0)]
    if case == "disappeared":
        assert scans[-1][1] >= 167 - 18


@pytest.mark.parametrize(("case", "starts", "reads"), [
    ("growing", [2], 1),
    ("number", [0, 1, 2], 4),
    ("low_confidence", [0, 1], 4),
    ("changed_prefix", [0, 1, 2], 3),
    ("one_sample", [0, 1, 2], 3),
])
def test_video_ocr_checks_growing_line_before_reading_crops(monkeypatch, case, starts, reads):
    import av
    from app.recognition import video_ocr

    def detect(image):
        y, x = np.nonzero(image[:, :, 0])
        left, top, right, bottom = x.min() - 1, y.min() - 1, x.max() + 2, y.max() + 2
        return SimpleNamespace(boxes=np.array([[[left, top], [right, top], [right, bottom], [left, bottom]]]))

    # 模拟同一行在相邻采样快速变宽，常规 IoU 匹配无法把它并为一条。
    widths = [49, 82, 135] if case == "one_sample" else [49, 82, 135, 135]
    samples = []
    for seconds, right in enumerate(widths):
        frame = np.zeros((64, 192, 3), dtype=np.uint8)
        frame[13:28, 22:right:3] = 255
        if case == "changed_prefix" and seconds >= 2:
            frame[13:28, 24:41] = 255
        samples.append((seconds, av.VideoFrame.from_ndarray(frame, format="bgr24")))

    batches = []

    def recognize(images):
        batches.extend(image.shape[1] for image in images)
        texts, scores = [], []
        for image in images:
            width = image.shape[1]
            if width > 80:
                texts.append("开始讲解这个现象的原因" + ("12" if case == "number" else ""))
            else:
                texts.append("开始讲解" if width < 40 else "开始讲解这个")
            scores.append(.89 if case == "low_confidence" and width > 80 else .99)
        return SimpleNamespace(txts=texts, scores=scores)

    monkeypatch.setattr(video_ocr, "alignment", lambda *_: (0, 0))
    monkeypatch.setattr(video_ocr, "changed_regions", lambda *_: [(0, 0, 192, 64)])
    engine = SimpleNamespace(_load_det_model=lambda: detect, cls_and_rotate=lambda images: (images, None),
                             recognize_txt=recognize)
    rows = video_ocr.recognize_lines(samples, lambda: engine, lambda: None, lambda _: None)
    assert len(batches) == reads
    assert [row["start_seconds"] for row in rows] == starts
    if case == "growing":
        assert rows == [{"text": "开始讲解这个现象的原因", "start_seconds": 2, "end_seconds": 3,
                         "confidence": pytest.approx(99)}]


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


def test_video_line_tracking_uses_a_fresh_ocr_cache_and_reuses_speech():
    settings = Settings()
    current = extraction_config(settings)
    assert uses_line_ocr(current)
    for version in ("extract-v4-video-line-ocr", "extract-v5-video-line-ocr", "extract-v6-video-line-ocr",
                    "extract-v7-video-line-ocr", "extract-v8-video-line-ocr"):
        previous = {**current, "extract_version": version}
        assert uses_line_ocr(previous)
        assert extraction_signature(current) != extraction_signature(previous)
        assert speech_signature(current) == speech_signature(previous)
