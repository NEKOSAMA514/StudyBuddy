import json
from pathlib import Path
import pytest
from app.video_download import bilibili_session as module
from app.video_download.bilibili_client import BilibiliError

IDENTIFIER = "b" * 32
SECRET = "unit_cookie_secret"


@pytest.fixture
def login(monkeypatch, tmp_path):
    state = {"code": 86101, "network_failed": False, "nav": True, "callback": None, "requests": []}
    class Client:
        def __init__(self, proxy=""):
            state["requests"].append(("proxy", proxy))
        def request(self, url, params=None, cookies=None):
            if state["network_failed"]:
                raise BilibiliError("network_failed")
            if url.endswith("/generate"):
                if state.get("generate_callback"):
                    state["generate_callback"]()
                return {"code": 0, "data": {"url": "https://passport.bilibili.com/test", "qrcode_key": "private-qr-key"}}, []
            if url.endswith("/spi"):
                return {"code": 0, "data": {"b_3": "test-buvid3"}}, []
            return {"code": 0, "data": {"code": state["code"]}}, [
                f"SESSDATA={SECRET}; HttpOnly; Secure", "bili_jct=unit_csrf; Path=/"]
        def navigation(self, cookies=None):
            if state["callback"]:
                state["callback"]()
            if state["network_failed"]:
                raise BilibiliError("network_failed")
            return {"code": 0 if state["nav"] else -101, "data": {"isLogin": state["nav"]}}
    monkeypatch.setattr(module, "BilibiliClient", Client)
    monkeypatch.setattr(module, "qr_image", lambda url: "data:image/png;base64,dGVzdA==")
    monkeypatch.setattr(module, "persistence_supported", lambda: True)
    monkeypatch.setattr(module, "protect", lambda value, decrypt=False: value[::-1])
    sessions = module.BilibiliSessions(tmp_path, lambda: "http://proxy.test:7890")
    return sessions, state


@pytest.mark.parametrize("code,expected", [(86101, "waiting"), (86090, "scanned"), (86038, "expired"), (12345, "unknown")])
def test_login_states_and_safe_public_result(login, code, expected):
    sessions, state = login
    created = sessions.start(IDENTIFIER)
    assert created["state"] == "waiting" and created["image"].startswith("data:image/png;")
    assert "private-qr-key" not in json.dumps(created)
    state["code"] = code
    result = sessions.poll(IDENTIFIER)
    assert result["state"] == expected and SECRET not in json.dumps(result)
    assert not sessions.path.exists()


def test_success_encrypted_reload_and_logout(login):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    result = sessions.poll(IDENTIFIER)
    assert result["state"] == "success" and result["session"]["persistent"]
    assert SECRET not in sessions.path.read_text() and SECRET not in json.dumps(result)
    assert sessions.cookies()["SESSDATA"] == SECRET
    assert sessions.cookies()["buvid3"] == "test-buvid3"
    restored = module.BilibiliSessions(sessions.path.parent.parent)
    assert restored.status()["state"] == "logged_in" and restored.cookies() == sessions.cookies()
    assert sessions.logout()["state"] == "logged_out"
    assert not sessions.path.exists() and sessions.cookies() == {}
    assert ("proxy", "http://proxy.test:7890") in state["requests"]


def test_save_failure_and_non_windows_do_not_claim_persistence(login, monkeypatch):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    def fail(*args, **kwargs):
        raise OSError("test save failure")
    monkeypatch.setattr(module, "protect", fail)
    result = sessions.poll(IDENTIFIER)
    assert result["state"] == "success" and not result["session"]["persistent"]
    assert "未能保存" in result["session"]["warning"]
    assert sessions.cookies()["SESSDATA"] == SECRET and not sessions.path.exists()
    monkeypatch.setattr(module, "persistence_supported", lambda: False)
    sessions.start("c" * 32)
    result = sessions.poll("c" * 32)
    assert not result["session"]["can_persist"] and not sessions.path.exists()


def test_network_failure_preserves_credentials_and_expired_login_is_distinct(login):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    sessions.poll(IDENTIFIER)
    state["network_failed"] = True
    assert sessions.status()["state"] == "unknown"
    assert sessions.cookies()["SESSDATA"] == SECRET and sessions.path.exists()
    state.update(network_failed=False, nav=False)
    assert sessions.status()["state"] == "expired" and not sessions.cookies()


@pytest.mark.parametrize("action", ["cancel", "logout", "new_login"])
def test_cancel_logout_or_new_session_during_confirmation_cannot_commit(login, action):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    def act():
        if action == "cancel":
            sessions.cancel(IDENTIFIER)
        elif action == "logout":
            sessions.logout()
        else:
            sessions.start("c" * 32)
    state["callback"] = act
    assert sessions.poll(IDENTIFIER)["state"] == "cancelled"
    assert not sessions.path.exists() and not sessions.cookies()


def test_delayed_status_cannot_restore_logout(login):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    sessions.poll(IDENTIFIER)
    state["callback"] = sessions.logout
    assert sessions.status()["state"] == "logged_out" and not sessions.cookies()


def test_cancel_before_start_and_after_expiry(login, monkeypatch):
    sessions, _ = login
    sessions.cancel(IDENTIFIER)
    assert sessions.start(IDENTIFIER)["state"] == "cancelled"
    sessions.start("c" * 32)
    now = module.time.monotonic()
    monkeypatch.setattr(module.time, "monotonic", lambda: now + 181)
    assert sessions.poll("c" * 32)["state"] == "expired"


def test_cancel_while_generating_cannot_publish_qr(login):
    sessions, state = login
    state["generate_callback"] = lambda: sessions.cancel(IDENTIFIER)
    result = sessions.start(IDENTIFIER)
    assert result["state"] == "cancelled" and "image" not in result


def test_expiring_during_confirmation_does_not_commit(login, monkeypatch):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    now = module.time.monotonic()
    state["callback"] = lambda: monkeypatch.setattr(module.time, "monotonic", lambda: now + 181)
    assert sessions.poll(IDENTIFIER)["state"] == "expired"
    assert not sessions.cookies() and not sessions.path.exists()


def test_unreadable_saved_credentials_do_not_break_app(tmp_path):
    path = tmp_path / "_private" / "bilibili-session.bin"
    path.parent.mkdir()
    path.write_bytes(b"not dpapi")
    sessions = module.BilibiliSessions(tmp_path)
    assert not sessions.cookies() and not sessions.persistent


def test_real_dpapi_save_reload_and_logout(login, monkeypatch):
    from app.util.private_storage import protect
    sessions, state = login
    monkeypatch.setattr(module, "protect", protect)
    sessions.start(IDENTIFIER)
    state["code"] = 0
    assert sessions.poll(IDENTIFIER)["session"]["persistent"]
    assert SECRET.encode() not in sessions.path.read_bytes()
    restarted = module.BilibiliSessions(sessions.path.parent.parent)
    assert restarted.cookies() == sessions.cookies()
    assert restarted.public()["persistent"]
    assert restarted.logout()["state"] == "logged_out"
    assert not sessions.path.exists() and not restarted.cookies()


def test_failed_logout_does_not_claim_success(login, monkeypatch):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    sessions.poll(IDENTIFIER)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == sessions.path:
            raise OSError("cannot remove")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError):
        sessions.logout()
    assert sessions.cookies() and sessions.public()["state"] == "logged_in"


def test_failed_atomic_save_and_logout_remove_encrypted_temporary(login, monkeypatch):
    sessions, state = login
    sessions.start(IDENTIFIER)
    state["code"] = 0
    original = Path.replace
    def fail(path, target):
        if target == sessions.path:
            raise OSError("replace failed")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", fail)
    assert not sessions.poll(IDENTIFIER)["session"]["persistent"]
    assert not sessions.path.with_suffix(".tmp").exists()
    sessions.path.with_suffix(".tmp").write_bytes(b"encrypted residue")
    sessions.logout()
    assert not sessions.path.with_suffix(".tmp").exists()


def test_api_local_only_no_store_and_no_secrets(client, app, login, monkeypatch):
    _, state = login
    assert client.get("/api/v1/bilibili/session", headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert client.post("/api/v1/bilibili/login", json={"id": "../bad"}).status_code == 422
    created = client.post("/api/v1/bilibili/login", json={"id": IDENTIFIER})
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["state"] == "waiting"
    state["code"] = 0
    result = client.post(f"/api/v1/bilibili/login/{IDENTIFIER}/poll")
    assert result.json()["state"] == "success"
    assert SECRET not in result.text and "SESSDATA" not in result.text
    assert client.delete("/api/v1/bilibili/session").json()["state"] == "logged_out"
    assert app.state.bilibili_sessions.cookies() == {}


def test_native_qr_generation_is_png():
    image = module.qr_image("https://passport.bilibili.com/test")
    import base64
    assert base64.b64decode(image.split(",")[1]).startswith(b"\x89PNG")
