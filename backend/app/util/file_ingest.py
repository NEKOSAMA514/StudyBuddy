"""保存二进制流，记录登记与重复文件的归属由调用方处理。"""
import hashlib
from pathlib import Path
from typing import BinaryIO
from ..errors import AppError


# 分块写入并累计大小与原件哈希，超过限制时由调用方清理本次上传。
def save_stream(stream: BinaryIO, destination: Path, max_bytes: int):
    size, fingerprint = 0, hashlib.sha256()
    output = destination.open("xb")
    try:
        with output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AppError("upload_limit", "文件超过大小限制。", 413)
                fingerprint.update(chunk)
                output.write(chunk)
        if not size:
            raise AppError("empty_file", "上传文件为空。")
        return {"size_bytes": size, "fingerprint": fingerprint.hexdigest()}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
