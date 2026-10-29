"""当前 Windows 账户的 DPAPI 存储；磁盘临时文件只包含密文。"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import sys
import uuid


class PrivateStorageError(OSError):
    """固定错误码，不将私有内容或底层异常文本带入日志。"""


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def persistence_supported():
    return sys.platform == "win32"


# 调用当前 Windows 账户的 DPAPI，并释放系统分配的输出缓冲区。
def protect(data, decrypt=False):
    if not persistence_supported():
        raise PrivateStorageError("dpapi_user_unavailable")

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise PrivateStorageError("credential_protection_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(target.data, ctypes.c_void_p))


def decode_private(data):
    """返回原始字节及是否为密文；旧备份和未完成临时文件保留逐字节内容。"""
    markers = (b'"storage_version"', b'"protection"', b'"ciphertext"')
    parsed = True
    try:
        envelope = json.loads(data)
    except (ValueError, UnicodeError):
        parsed = False
    if not parsed:
        if any(marker in data for marker in markers):
            raise PrivateStorageError("private_envelope_invalid")
        return data, False
    if not isinstance(envelope, dict) or not any(key in envelope for key in ("storage_version", "protection", "ciphertext")):
        return data, False
    try:
        if (set(envelope) != {"storage_version", "protection", "ciphertext"}
                or type(envelope["storage_version"]) is not int or envelope["storage_version"] != 1
                or envelope["protection"] != "dpapi-user"):
            raise ValueError()
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        return protect(ciphertext, decrypt=True), True
    except Exception:
        pass
    # 在 except 外抛出，避免 HTTP 中间件重新接上包含私有内容的 __context__。
    raise PrivateStorageError("private_decryption_failed")


# 密文先在内存中回解核对，再包装成可版本校验的 JSON 字节。
def encode_private(data):
    try:
        encrypted = protect(data)
        if protect(encrypted, decrypt=True) != data:
            raise ValueError()
        return json.dumps({"storage_version": 1, "protection": "dpapi-user",
                           "ciphertext": base64.b64encode(encrypted).decode("ascii")}).encode("utf-8")
    except Exception:
        pass
    raise PrivateStorageError("private_encryption_failed")


# 兼容旧明文与新密文读取，读取失败只返回固定错误码。
def read_private_json(path):
    try:
        payload, _ = decode_private(path.read_bytes())
        return json.loads(payload)
    except PrivateStorageError:
        raise
    except Exception:
        pass
    raise PrivateStorageError("private_json_invalid")


# 将密文写入独占临时文件并刷盘，再原子替换目标，失败保留原文件。
def write_encrypted(path, encrypted):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    failed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except Exception:
        failed = True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # 替换失败时留下的也只有密文，下次启动按已知临时文件迁移。
    if failed:
        raise PrivateStorageError("private_write_failed")


def write_private_bytes(path, data):
    write_encrypted(path, encode_private(data))


def write_private_json(path, value):
    write_private_bytes(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))
