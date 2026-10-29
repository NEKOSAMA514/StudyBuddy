"""扫码会话和本机登录凭据；公开接口不返回 Cookie。"""
import base64
from http.cookies import SimpleCookie
import io
import json
import os
import re
import threading
import time
from fastapi import APIRouter
from pydantic import BaseModel, Field
from .bilibili_client import BilibiliClient, BilibiliError, COOKIE_NAMES
from ..util.private_storage import persistence_supported, protect


def qr_image(url):
    import qrcode
    output = io.BytesIO()
    qrcode.make(url).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class BilibiliSessions:
    def __init__(self, data_dir, proxy=lambda: ""):
        self.path = data_dir / "_private" / "bilibili-session.bin"
        self.proxy = proxy
        self.lock = threading.RLock()
        self.records = {}
        self.current = None
        self.revision = 0
        self.state, self.warning, self.persistent = "logged_out", "", False
        self._cookies = {}
        if self.path.exists() and persistence_supported():
            try:
                self._cookies = json.loads(protect(self.path.read_bytes(), decrypt=True))
                if not self._cookies.get("SESSDATA"):
                    raise ValueError()
                self.state, self.persistent = "unknown", True
            except Exception:
                self.warning = "已保存的 B 站登录无法读取，请重新登录。"

    def cookies(self):
        with self.lock:
            return dict(self._cookies)

    def public(self):
        return {"state": self.state, "persistent": self.persistent,
                "can_persist": persistence_supported(), "warning": self.warning}

    # 在锁外验证登录，再按会话修订号发布结果，防止覆盖期间发生的退出。
    def status(self):
        with self.lock:
            cookies, revision = self.cookies(), self.revision
            if not cookies:
                return self.public()
        try:
            nav = BilibiliClient(self.proxy()).navigation(cookies)
            state = "logged_in" if (nav.get("data") or {}).get("isLogin") else (
                "expired" if nav.get("code") == -101 or nav.get("code") == 0 else "unknown")
        except BilibiliError:
            state = "unknown"
        with self.lock:
            if revision == self.revision:
                self.state = state
                if state == "expired":
                    self._cookies = {}
            return self.public()

    def _record(self, identifier):
        record = self.records.get(identifier)
        if not record:
            return {"id": identifier, "state": "expired"}
        if record["expires"] <= time.monotonic():
            record["state"] = "expired"
            record.pop("cookies", None)
            record.pop("key", None)
        return record

    # 每次新扫码取消旧登录尝试，二维码和临时凭据只归属当前记录。
    def start(self, identifier):
        with self.lock:
            now = time.monotonic()
            self.records = {key: value for key, value in self.records.items() if value["expires"] > now}
            if identifier in self.records:
                return {"id": identifier, "state": self._record(identifier)["state"]}
            for old in self.records.values():
                old["state"] = "cancelled"
                old.pop("cookies", None)
                old.pop("key", None)
            record = {"id": identifier, "state": "creating", "expires": now + 180}
            self.records[identifier] = record
            self.current = identifier
        try:
            response = BilibiliClient(self.proxy()).request(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                {"source": "main-fe-header"})[0]
            data = response.get("data") or {}
            if response.get("code") != 0 or not data.get("qrcode_key") or not data.get("url"):
                raise BilibiliError("qr_unavailable")
            image = qr_image(data["url"])
        except Exception:
            with self.lock:
                if record["state"] == "creating":
                    record["state"] = "failed"
            return {"id": identifier, "state": record["state"]}
        with self.lock:
            if self._record(identifier) is not record or record["state"] != "creating":
                return {"id": identifier, "state": "cancelled"}
            record.update(state="waiting", key=data["qrcode_key"])
            return {"id": identifier, "state": "waiting", "image": image, "expires_in": 180}

    # 验证扫码结果和登录有效性后才发布会话，支持时沿用 DPAPI 密文保存。
    def poll(self, identifier):
        with self.lock:
            record = self._record(identifier)
            if record["state"] not in {"waiting", "scanned", "unknown"} or self.current != identifier:
                return {"id": identifier, "state": record["state"], **({"session": self.public()} if record["state"] == "success" else {})}
            candidate = record.get("cookies")
            key = record["key"]
        try:
            client = BilibiliClient(self.proxy())
            if not candidate:
                payload, headers = client.request(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    {"qrcode_key": key, "source": "main-fe-header"})
                data = payload.get("data") or {}
                code = data.get("code") if payload.get("code") == 0 else None
                state = {86101: "waiting", 86090: "scanned", 86038: "expired", 0: "success"}.get(code, "unknown")
                if state == "success":
                    candidate = {}
                    for header in headers:
                        parsed = SimpleCookie()
                        parsed.load(header)
                        candidate.update({name: parsed[name].value for name in COOKIE_NAMES if name in parsed})
                    if not candidate.get("SESSDATA"):
                        raise BilibiliError("credentials_missing")
                    if not candidate.get("buvid3"):
                        try:
                            fingerprint = client.request("https://api.bilibili.com/x/frontend/finger/spi")[0]
                            buvid = (fingerprint.get("data") or {}).get("b_3")
                            if buvid:
                                candidate["buvid3"] = buvid
                        except BilibiliError:
                            pass
            else:
                state = "success"
            if state == "success" and not (client.navigation(candidate).get("data") or {}).get("isLogin"):
                state = "failed"
        except BilibiliError:
            state = "unknown"
        with self.lock:
            if (self._record(identifier) is not record or self.current != identifier
                    or record["state"] not in {"waiting", "scanned", "unknown"}):
                state = self._record(identifier)["state"] if self.current == identifier else "cancelled"
                return {"id": identifier, "state": state, **({"session": self.public()} if state == "success" else {})}
            if candidate:
                record["cookies"] = candidate
            record["state"] = state
            if state == "success":
                self._cookies, self.state = candidate, "logged_in"
                self.revision += 1
                self.persistent, self.warning = False, ""
                if persistence_supported():
                    try:
                        encrypted = protect(json.dumps(candidate).encode())
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.path.with_suffix(".tmp")
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "wb") as file:
                            file.write(encrypted)
                        temporary.replace(self.path)
                        self.persistent = True
                    except Exception:
                        self.warning = "登录成功，但未能保存到本机；仅本次运行有效。"
                        try:
                            self.path.with_suffix(".tmp").unlink(missing_ok=True)
                        except OSError:
                            pass
                else:
                    self.warning = "当前系统仅支持本次运行登录，重启后需重新扫码。"
                record.pop("cookies", None)
                record.pop("key", None)
            return {"id": identifier, "state": state, **({"session": self.public()} if state == "success" else {})}

    def cancel(self, identifier):
        with self.lock:
            self.records[identifier] = {"id": identifier, "state": "cancelled", "expires": time.monotonic() + 180}
            if self.current == identifier:
                self.current = None
            return {"state": "cancelled"}

    def logout(self):
        with self.lock:
            # 删除失败时保留原状态，避免界面声称已退出但重启后又恢复。
            self.path.with_suffix(".tmp").unlink(missing_ok=True)
            self.path.unlink(missing_ok=True)
            self._cookies = {}
            self.revision += 1
            self.state, self.warning, self.persistent = "logged_out", "", False
            self.records.clear()
            self.current = None
            return self.public()


class LoginInput(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


def bilibili_router(sessions):
    router = APIRouter(prefix="/api/v1/bilibili", tags=["bilibili"])
    router.add_api_route("/session", sessions.status, methods=["GET"])
    router.add_api_route("/session", sessions.logout, methods=["DELETE"])

    @router.post("/login")
    def start(value: LoginInput):
        return sessions.start(value.id)

    @router.post("/login/{identifier}/poll")
    def poll(identifier: str):
        return sessions.poll(identifier)

    @router.delete("/login/{identifier}")
    def cancel(identifier: str):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            return {"state": "cancelled"}
        return sessions.cancel(identifier)
    return router
