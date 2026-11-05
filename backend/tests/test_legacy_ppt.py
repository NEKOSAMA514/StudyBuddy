import struct
from pathlib import Path

import olefile
import pytest

from app.documents.extraction import extract
from app.documents.legacy_ppt import _objects
from app.errors import AppError, TaskInterrupted
from .document_samples import sample

FIXTURES = Path(__file__).parent / "fixtures/ppt"


def extracted(tmp_path, name="basic_test_ppt_file.ppt"):
    path = tmp_path / "source.ppt"
    path.write_bytes((FIXTURES / name).read_bytes())
    return extract(path, "ppt")


def test_legacy_ppt_preserves_slide_text_notes_and_source_locations(tmp_path):
    content = extracted(tmp_path)
    first, second = content["units"]
    assert [unit["locator"] for unit in content["units"]] == [{"slide": 1}, {"slide": 2}]
    assert first["label"] == "幻灯片 1" and second["label"] == "幻灯片 2"
    assert "This is a test title" in first["text"]
    assert "讲者备注：" in first["text"] and "These are the notes for page 1" in first["text"]
    assert "This is the title on page 2" in second["text"]
    assert "These are the notes on page two" in second["text"]
    assert content["character_count"] == sum(len(unit["text"]) for unit in content["units"])
    assert any("图片文字" in warning for warning in content["warnings"])


def test_legacy_ppt_uses_presentation_order_not_storage_order(tmp_path):
    units = extracted(tmp_path, "incorrect_slide_order.ppt")["units"]
    assert [unit["text"].splitlines()[0] for unit in units] == ["Slide 1", "Slide 2", "Slide 3"]
    assert "Third slide I added" in units[1]["text"]
    assert "Second slide I added" in units[2]["text"]


def test_legacy_ppt_decodes_unicode_and_supplementary_characters(tmp_path):
    text = extracted(tmp_path, "54880_chinese.ppt")["units"][0]["text"]
    assert "複数の文字" in text and "表Mixパﾋﾟ𠮟" in text


@pytest.mark.parametrize("raw", [b"broken PPT", sample("pptx"), (FIXTURES / "Password_Protected-hello.ppt").read_bytes()],
                         ids=["corrupt", "mislabeled", "encrypted"])
def test_legacy_ppt_rejects_corrupt_mislabeled_and_encrypted_files(tmp_path, raw):
    path = tmp_path / "source.ppt"
    path.write_bytes(raw)
    with pytest.raises(AppError) as error:
        extract(path, "ppt")
    assert error.value.code == "invalid_document"


def test_legacy_ppt_rejects_cyclic_edit_history(tmp_path):
    path = tmp_path / "cycle.ppt"
    path.write_bytes(sample("ppt"))
    with olefile.OleFileIO(path, write_mode=True) as source:
        current = source.openstream("Current User").read()
        offset = struct.unpack_from("<I", current, 16)[0]
        data = bytearray(source.openstream("PowerPoint Document").read())
        struct.pack_into("<I", data, offset + 16, offset)
        source.write_stream("PowerPoint Document", bytes(data))
    with pytest.raises(AppError) as error:
        extract(path, "ppt")
    assert error.value.code == "invalid_document"


def test_legacy_ppt_respects_slide_limit_and_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.documents.extraction.MAX_UNITS", 1)
    with pytest.raises(AppError) as error:
        extracted(tmp_path)
    assert error.value.code == "document_limit"
    monkeypatch.setattr("app.documents.extraction.MAX_UNITS", 10000)

    class Cancelled:
        def check(self):
            raise TaskInterrupted()

    with pytest.raises(TaskInterrupted):
        extract(tmp_path / "source.ppt", "ppt", Cancelled())


def test_legacy_ppt_checks_declared_stream_size_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(olefile.OleFileIO, "get_size", lambda *args: 129 * 1024 ** 2)
    with pytest.raises(AppError) as error:
        extracted(tmp_path)
    assert error.value.code == "document_limit"


def test_legacy_ppt_latest_objects_override_old_versions_and_inherit_unchanged_objects():
    def record(kind, payload):
        return struct.pack("<HHI", 0, kind, len(payload)) + payload

    data = bytearray(b"\0" * 8)
    old_directory = len(data)
    data += record(6002, struct.pack("<III", (2 << 20) | 1, 100, 200))
    old_edit = len(data)
    data += record(4085, struct.pack("<IIIIIII", 0, 0, 0, old_directory, 1, 3, 0))
    new_directory = len(data)
    data += record(6002, struct.pack("<II", (1 << 20) | 1, 300))
    new_edit = len(data)
    data += record(4085, struct.pack("<IIIIIII", 0, 0, old_edit, new_directory, 1, 3, 0))
    objects, document = _objects(bytes(data), new_edit, lambda: None)
    assert objects == {1: 300, 2: 200} and document == 1
