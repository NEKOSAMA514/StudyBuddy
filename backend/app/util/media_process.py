import subprocess
import time
from ..errors import AppError


# 有限等待媒体子进程，取消、超时或失败时统一回收进程资源。
def command(args: list[str], timeout: int = 300, cancel=None):
    if args[0] in {"ffmpeg", "ffprobe"}:
        args = [args[0], "-protocol_whitelist", "file,pipe", *args[1:]]
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise AppError("media_cancelled", "播放视频准备已取消。")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
                return stdout
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
    except FileNotFoundError as exc:
        raise AppError("tool_missing", f"缺少本地工具 {args[0]}，请在当前 Conda 环境中安装并检查 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("media_timeout", "视频处理超时，请尝试更短的视频。") from exc
    except subprocess.CalledProcessError as exc:
        raise AppError("invalid_media", "文件无法解码或媒体处理失败，请检查视频是否损坏。") from exc
