"""真实 DPAPI 与隔离目录，不读取正式凭据，也不调用外部服务。"""
import ctypes
from ctypes import wintypes
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.settings.private_migration import migrate_private_settings
from app.util import private_storage as storage
from app.video_download.bilibili_session import BilibiliSessions

SECRETS = ("isolated-model-credential-A", "isolated-model-credential-B", "isolated-mineru-credential")


def payload(path):
    return storage.decode_private(path.read_bytes())[0]


def assert_encrypted(folder):
    for path in folder.iterdir():
        if path.is_file():
            assert all(secret.encode() not in path.read_bytes() for secret in SECRETS), path.name


@pytest.mark.parametrize("data", [b"", b"isolated-credential\x00with-nul", "隔离密钥和多行\n内容".encode()])
def test_real_dpapi_round_trip(data):
    encrypted = storage.protect(data)
    assert encrypted != data
    assert storage.protect(encrypted, decrypt=True) == data


def test_existing_bilibili_ciphertext_remains_compatible(tmp_path):
    # 与迁移前实现相同的原始 DPAPI 二进制格式，不经过新 JSON 包装。
    class OldBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = json.dumps({"SESSDATA": SECRETS[0], "bili_jct": SECRETS[1]}).encode()
    buffer = ctypes.create_string_buffer(raw)
    source = OldBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = OldBlob()
    assert ctypes.WinDLL("crypt32").CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    try:
        ciphertext = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))
    path = tmp_path / "_private/bilibili-session.bin"
    path.parent.mkdir()
    path.write_bytes(ciphertext)
    sessions = BilibiliSessions(tmp_path)
    assert sessions.cookies()["SESSDATA"] == SECRETS[0]
    assert sessions.public()["persistent"]
    assert path.read_bytes() == ciphertext


def test_envelope_contains_only_storage_metadata_and_ciphertext(tmp_path):
    path = tmp_path / "model-settings.json"
    value = {"token": SECRETS[0], "summary_prompt": "原始配置", "nested": [SECRETS[1], SECRETS[2]]}
    storage.write_private_json(path, value)
    envelope = json.loads(path.read_bytes())
    assert set(envelope) == {"storage_version", "protection", "ciphertext"}
    assert envelope["storage_version"] == 1 and envelope["protection"] == "dpapi-user"
    assert storage.read_private_json(path) == value
    assert_encrypted(tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []


def legacy_files(tmp_path):
    folder = tmp_path / "_private"
    folder.mkdir()
    files = {
        "model-settings.json": json.dumps({"token": SECRETS[0], "model": "test-model", "summary_prompt": "原要求"}).encode(),
        "mineru.json": json.dumps({"token": SECRETS[2]}).encode(),
        "model-settings.rollback-backup-20261012-053350.json": ('  {"token": "' + SECRETS[1] + '"}\n').encode(),
        "model-settings.json.before-input-limit-20261027000000.bak": json.dumps({"token": SECRETS[0], "outline_text_limit": 1200}).encode(),
        "model-settings.json.bak": SECRETS[0].encode(),
        "model-settings.bak": SECRETS[1].encode(),
        "mineru.json.bak": SECRETS[2].encode(),
        "mineru.bak": SECRETS[2].encode(),
        "model-settings.tmp": ('{"token":"' + SECRETS[1] + '","unfinished":').encode(),
        "mineru.json.tmp": ('{"token":"' + SECRETS[2] + '"').encode(),
        ".model-settings.json.interrupted.tmp": SECRETS[0].encode(),
    }
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder, files


def test_migration_preserves_exact_backup_and_temporary_content_and_is_idempotent(tmp_path):
    folder, files = legacy_files(tmp_path)
    untouched = {"network-settings.json": b'{"enabled":false,"url":""}',
                 "recognition-settings.json": b'{"asr_option":"paraformer"}', "bilibili-session.bin": b"existing-bilibili-cipher"}
    for name, data in untouched.items():
        (folder / name).write_bytes(data)
    settings = Settings(data_dir=tmp_path)
    assert migrate_private_settings(settings) is settings
    assert set(path.name for path in folder.iterdir()) == set(files) | set(untouched)
    for name, data in files.items():
        assert payload(folder / name) == data
    for name, data in untouched.items():
        assert (folder / name).read_bytes() == data
    snapshot = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}
    migrate_private_settings(settings)
    assert snapshot == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.iterdir()}
    assert_encrypted(folder)


@pytest.mark.parametrize("name,secret,field", [("model-settings.json", SECRETS[0], "llm_key"), ("mineru.json", SECRETS[2], "mineru_token")])
@pytest.mark.parametrize("cut", [0, 5, 25, 80, -12, None])
def test_uncommitted_writer_temporary_is_quarantined_and_restart_succeeds(tmp_path, caplog, name, secret, field, cut):
    from app.settings.settings_service import load_application_settings
    folder = tmp_path / "data/_private"
    folder.mkdir(parents=True)
    main = folder / name
    storage.write_private_json(main, {"token": secret})
    original = main.read_bytes()
    temporary = folder / f".{name}.{'a' * 32}.tmp"
    pending = storage.encode_private(json.dumps({"token": SECRETS[1]}).encode())
    fragment = pending if cut is None else pending[:cut]
    temporary.write_bytes(fragment)
    settings = Settings(data_dir=folder.parent, model_dir=tmp_path / "models", log_dir=tmp_path / "log")
    for _ in range(2):
        loaded = load_application_settings(settings)
        assert getattr(loaded, field) == secret
        assert main.read_bytes() == original
    retained = list((folder / "_uncommitted").iterdir())
    assert len(retained) == 1 and retained[0].read_bytes() == fragment
    assert not temporary.exists()
    notices = [record.message for record in caplog.records if "private_uncommitted_file_quarantined" in record.message]
    assert len(notices) == 1
    assert all(value not in notices[0] for value in SECRETS)


@pytest.mark.parametrize("case", ["missing-main", "invalid-main", "legacy-temporary", "backup", "unknown-id"])
def test_quarantine_does_not_hide_invalid_main_or_other_credential_files(tmp_path, case):
    folder = tmp_path / "_private"
    folder.mkdir()
    main = folder / "model-settings.json"
    if case != "missing-main": storage.write_private_json(main, {"token": SECRETS[0]})
    if case == "invalid-main": main.write_bytes(b"invalid main")
    name = f".model-settings.json.{'b' * 32}.tmp"
    if case == "legacy-temporary": name = "model-settings.tmp"
    if case == "backup": name = "model-settings.json.bak"
    if case == "unknown-id": name = ".model-settings.json.unknown.tmp"
    temporary = folder / name
    temporary.write_bytes(storage.encode_private(b"pending")[:80])
    original = {path.name: path.read_bytes() for path in folder.iterdir()}
    with pytest.raises(storage.PrivateStorageError): migrate_private_settings(Settings(data_dir=tmp_path))
    assert original == {path.name: path.read_bytes() for path in folder.iterdir()}


@pytest.mark.parametrize("target", ["model-settings.json.before-input-limit-20261028.bak", "model-settings.rollback-backup-20261028.json", "mineru.json.bak", "model-settings.tmp"])
def test_uncommitted_backup_write_is_isolated_without_overwriting_retained_files(tmp_path, target):
    folder = tmp_path / "_private"
    main = folder / ("mineru.json" if target.startswith("mineru") else "model-settings.json")
    storage.write_private_json(main, {"token": SECRETS[0]})
    original = main.read_bytes()
    temporary = folder / f".{target}.{'c' * 32}.tmp"
    fragment = storage.encode_private(b"pending")[:80]
    temporary.write_bytes(fragment)
    retained = folder / "_uncommitted"
    retained.mkdir()
    (retained / "old.quarantined").write_bytes(b"keep")
    migrate_private_settings(Settings(data_dir=tmp_path))
    assert main.read_bytes() == original and not temporary.exists()
    assert (retained / "old.quarantined").read_bytes() == b"keep"
    assert sorted(path.read_bytes() for path in retained.iterdir()) == sorted([b"keep", fragment])


def test_failed_quarantine_preserves_main_and_temporary_and_can_retry(tmp_path, monkeypatch, caplog):
    folder = tmp_path / "_private"
    main = folder / "model-settings.json"
    storage.write_private_json(main, {"token": SECRETS[0]})
    original = main.read_bytes()
    temporary = folder / f".model-settings.json.{'d' * 32}.tmp"
    fragment = storage.encode_private(b"pending")[:80]
    temporary.write_bytes(fragment)
    with monkeypatch.context() as scoped:
        def fail(*args, **kwargs): raise OSError(SECRETS[1])
        scoped.setattr(Path, "rename", fail)
        with pytest.raises(storage.PrivateStorageError, match="private_migration_failed") as error:
            migrate_private_settings(Settings(data_dir=tmp_path))
    assert error.value.__context__ is None
    assert main.read_bytes() == original and temporary.read_bytes() == fragment
    assert not caplog.records
    migrate_private_settings(Settings(data_dir=tmp_path))
    assert main.read_bytes() == original
    assert [path.read_bytes() for path in (folder / "_uncommitted").iterdir()] == [fragment]


def test_invalid_canonical_config_stops_before_migrating_any_file(tmp_path):
    folder, _ = legacy_files(tmp_path)
    (folder / "mineru.json").write_text(json.dumps({"token": SECRETS[2], "unexpected": True}))
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    with pytest.raises(storage.PrivateStorageError, match="private_settings_invalid"):
        migrate_private_settings(Settings(data_dir=tmp_path))
    assert before == {path.name: path.read_bytes() for path in folder.iterdir()}


def test_interrupted_migration_keeps_completed_ciphertext_and_resumes(tmp_path, monkeypatch):
    folder, files = legacy_files(tmp_path)
    original = Path.replace
    count = 0
    def fail_second(path, target):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError(SECRETS[0])
        return original(path, target)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "replace", fail_second)
        with pytest.raises(storage.PrivateStorageError, match="private_migration_failed"):
            migrate_private_settings(Settings(data_dir=tmp_path))
    done = {path: path.read_bytes() for path in folder.iterdir() if storage.decode_private(path.read_bytes())[1]}
    assert len(done) == 1
    assert all(payload(folder / name) == data for name, data in files.items())
    migrate_private_settings(Settings(data_dir=tmp_path))
    assert all(path.read_bytes() == data for path, data in done.items())
    assert all(payload(folder / name) == data for name, data in files.items())
    assert_encrypted(folder)


@pytest.mark.parametrize("corruption", ["base64", "ciphertext", "version", "scheme", "missing", "truncated", "json", "access"])
def test_damaged_ciphertext_blocks_startup_without_environment_fallback(tmp_path, corruption, monkeypatch):
    folder = tmp_path / "data/_private"
    folder.mkdir(parents=True)
    path = folder / "model-settings.json"
    storage.write_private_json(path, {"token": SECRETS[0]})
    value = json.loads(path.read_bytes())
    if corruption == "base64": value["ciphertext"] = "not base64!!!"
    if corruption == "ciphertext": value["ciphertext"] = "bm90IGRwYXBp"
    if corruption == "version": value["storage_version"] = 9
    if corruption == "scheme": value["protection"] = "unsupported"
    if corruption == "missing": value.pop("protection")
    if corruption == "json": monkeypatch.setattr(storage, "protect", lambda *a, **kw: SECRETS[0].encode())
    if corruption == "access":
        def denied(*args, **kwargs): raise OSError(SECRETS[0])
        monkeypatch.setattr(storage, "protect", denied)
    damaged = json.dumps(value).encode()
    if corruption == "truncated": damaged = damaged[:-12]
    path.write_bytes(damaged)
    with pytest.raises(AppError) as error:
        create_app(Settings(data_dir=folder.parent, log_dir=tmp_path / "log", model_dir=tmp_path / "models",
                            static_dir=tmp_path / "absent", llm_key="env-must-not-hide-error"), start_worker=False)
    assert error.value.code == "settings_load_failed"
    assert path.read_bytes() == damaged
    assert all(secret not in str(error.value) for secret in SECRETS)
    assert all(secret not in file.read_text("utf-8") for file in (tmp_path / "log").glob("*.log") for secret in SECRETS)
    assert not (folder.parent / "studybuddy.sqlite3").exists()


@pytest.mark.parametrize("failure", ["encrypt", "open", "replace"])
def test_startup_migration_failure_keeps_originals_and_does_not_start_worker(tmp_path, monkeypatch, failure):
    folder, _ = legacy_files(tmp_path)
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    started = []
    monkeypatch.setattr("app.analysis.worker.Worker.start", lambda self: started.append(True))
    def fail(*args, **kwargs): raise OSError(SECRETS[1])
    with monkeypatch.context() as scoped:
        if failure == "encrypt": scoped.setattr(storage, "protect", fail)
        elif failure == "open": scoped.setattr(storage.os, "open", fail)
        else: scoped.setattr(Path, "replace", fail)
        with pytest.raises(AppError) as error:
            create_app(Settings(data_dir=tmp_path, log_dir=tmp_path / "log", model_dir=tmp_path / "models", static_dir=tmp_path / "absent"))
    assert error.value.code == "settings_load_failed"
    assert not started and not (tmp_path / "studybuddy.sqlite3").exists()
    assert before == {path.name: path.read_bytes() for path in folder.iterdir()}
    assert all(secret not in file.read_text("utf-8") for file in (tmp_path / "log").glob("*.log") for secret in SECRETS)
    migrate_private_settings(Settings(data_dir=tmp_path))
    assert_encrypted(folder)


def test_unavailable_dpapi_never_saves_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "persistence_supported", lambda: False)
    path = tmp_path / "settings.json"
    with pytest.raises(storage.PrivateStorageError, match="private_encryption_failed"):
        storage.write_private_json(path, {"token": SECRETS[0]})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["encrypt", "verify", "open", "write", "fsync", "replace"])
@pytest.mark.parametrize("area", ["model", "mineru"])
def test_failed_save_preserves_file_current_snapshot_and_redacts_logs(client, app, monkeypatch, failure, area):
    endpoint = "/api/v1/settings/" + area
    secret = SECRETS[0] if area == "model" else SECRETS[2]
    assert client.put(endpoint, json={"token": secret}).status_code == 200
    current = app.state.settings_service.current
    folder = current.data_dir / "_private"
    path = folder / ("model-settings.json" if area == "model" else "mineru.json")
    before = path.read_bytes()
    def fail(*args, **kwargs): raise OSError(SECRETS[1])
    with monkeypatch.context() as scoped:
        if failure == "encrypt": scoped.setattr(storage, "protect", fail)
        elif failure == "verify":
            original = storage.protect
            scoped.setattr(storage, "protect", lambda data, decrypt=False: b"wrong" if decrypt else original(data))
        elif failure == "open": scoped.setattr(storage.os, "open", fail)
        elif failure == "fsync": scoped.setattr(storage.os, "fsync", fail)
        elif failure == "replace": scoped.setattr(Path, "replace", fail)
        else:
            fdopen = os.fdopen
            class InterruptedWrite:
                def __init__(self, *args, **kwargs): self.file = fdopen(*args, **kwargs)
                def __enter__(self): return self
                def __exit__(self, *args): self.file.close()
                def write(self, data):
                    self.file.write(data[:20])
                    fail()
            scoped.setattr(storage.os, "fdopen", InterruptedWrite)
        response = client.put(endpoint, json={"token": SECRETS[1]})
    assert response.status_code == 500
    assert app.state.settings_service.current is current
    assert path.read_bytes() == before
    assert all(value not in response.text for value in SECRETS)
    for file in current.log_dir.glob("*.log"):
        assert all(value not in file.read_text("utf-8") for value in SECRETS)
    assert_encrypted(folder)
    assert not list(folder.glob("*.tmp"))
    assert client.put(endpoint, json={"token": SECRETS[1]}).status_code == 200


def test_multiple_profiles_mineru_clear_and_reload_remain_unchanged(client, app):
    assert client.put("/api/v1/settings/model", json={"token": SECRETS[0]}).status_code == 200
    response = client.post("/api/v1/settings/model/profiles", json={"name": "second", "token": SECRETS[1]})
    assert response.status_code == 201
    second = response.json()["profiles"][-1]["id"]
    assert client.put("/api/v1/settings/mineru", json={"token": SECRETS[2]}).status_code == 200
    settings = app.state.settings_service.current
    from app.settings.settings_service import load_application_settings
    loaded = load_application_settings(replace(settings, llm_key="env", mineru_token="env", llm_profiles=()))
    assert loaded.llm_key == SECRETS[0] and loaded.mineru_token == SECRETS[2]
    assert {value["token"] for value in loaded.llm_profiles} == set(SECRETS[:2])
    assert client.put(f"/api/v1/settings/model/profiles/{second}", json={"name": "second"}).status_code == 200
    assert app.state.settings_service.current.llm_profiles[-1]["token"] == SECRETS[1]
    assert client.put("/api/v1/settings/model", json={"clear_token": True}).status_code == 200
    assert client.put("/api/v1/settings/mineru", json={"clear_token": True}).status_code == 200
    assert client.delete(f"/api/v1/settings/model/profiles/{second}").status_code == 200
    loaded = load_application_settings(replace(app.state.settings_service.current, llm_key="env", mineru_token="env", llm_profiles=()))
    assert loaded.llm_key == loaded.mineru_token == ""
    assert_encrypted(settings.data_dir / "_private")
