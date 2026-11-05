import json
import sys
from pathlib import Path

import pytest

from app.util import json_io


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_json_retries_windows_file_occupancy_without_rewriting_or_publishing_partial_data(tmp_path, monkeypatch, winerror):
    target = tmp_path / "state.json"
    target.write_text('{"progress": 1}', encoding="utf-8")
    previous = target.read_bytes()
    attempts, waits = [], []
    replace = Path.replace

    def occupied(source, destination):
        attempts.append(source)
        assert target.read_bytes() == previous
        assert json.loads(source.read_text(encoding="utf-8")) == {"progress": 2}
        if len(attempts) <= 2:
            error = PermissionError("temporary Windows file occupancy")
            error.winerror = winerror
            raise error
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", occupied)
    monkeypatch.setattr(json_io, "sleep", waits.append)
    json_io.atomic_json(target, {"progress": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"progress": 2}
    assert len(attempts) == 3 and waits == [0.05, 0.1]
    assert not target.with_suffix(".tmp").exists()


def test_atomic_json_stops_retrying_persistent_windows_failure_and_preserves_old_state(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"progress": 1}', encoding="utf-8")
    attempts, waits = [], []

    def denied(source, destination):
        attempts.append(source)
        error = PermissionError("persistent denial")
        error.winerror = 5
        raise error

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(json_io, "sleep", waits.append)
    with pytest.raises(PermissionError, match="persistent denial"):
        json_io.atomic_json(target, {"progress": 2})
    assert len(attempts) == 6 and len(waits) == 5
    assert json.loads(target.read_text(encoding="utf-8")) == {"progress": 1}
    assert json.loads(target.with_suffix(".tmp").read_text(encoding="utf-8")) == {"progress": 2}


@pytest.mark.parametrize("error", [PermissionError("permission denied"), FileNotFoundError("missing"), OSError("disk error")])
def test_atomic_json_does_not_retry_other_errors(tmp_path, monkeypatch, error):
    waits = []

    def fail(*args):
        raise error

    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(json_io, "sleep", waits.append)
    with pytest.raises(type(error)):
        json_io.atomic_json(tmp_path / "state.json", {"progress": 2})
    assert waits == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows disallows replacement while an ordinary reader is open")
def test_atomic_json_recovers_from_real_windows_reader_lock(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"progress": 1}', encoding="utf-8")
    waits = []
    with target.open("rb") as reader:
        def release(delay):
            waits.append(delay)
            reader.close()
        monkeypatch.setattr(json_io, "sleep", release)
        json_io.atomic_json(target, {"progress": 2})
    assert waits == [0.05]
    assert json.loads(target.read_text(encoding="utf-8")) == {"progress": 2}
