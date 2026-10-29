"""下载器进程的有限等待与进程树清理。"""
from collections import deque
import os
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from ..errors import AppError

logger = logging.getLogger("studybuddy")


def downloader_args(proxy="", url=""):
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--proxy", proxy, "--no-warnings", "--no-playlist",
            "--playlist-items", "1", "--no-cache-dir", "--encoding", "utf-8", "--socket-timeout", "30",
            "--retries", "3", "--fragment-retries", "3", "--extractor-retries", "3"]
    host = (urlsplit(url).hostname or "").lower()
    if host == "bilibili.com" or host.endswith(".bilibili.com") or host in {"b23.tv", "www.b23.tv"}:
        # 部分随机 Chrome 版本会让 B 站返回成功但无视频流；解析和下载使用同一已验证标识。
        args += ["--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"]
    return args


def format_selector():
    return "bestvideo*+bestaudio/best/bestvideo"


def network_error_hint(url, error, proxy):
    if error.code not in {"video_download_failed", "video_download_timeout"}:
        return error
    message = error.message
    status = re.search(r"\bHTTP(?:\s*Error)?\s*:?\s*(\d{3})\b", message, re.I)
    if (status and status[1] in {"404", "410"}) or re.search(
            r"deleted|removed|(?:video|resource|media) not found|does not exist|unavailable video|invalid (?:url|link)|视频已删除|资源不存在", message, re.I):
        return error
    network = error.code == "video_download_timeout" or re.search(
        r"timed?\s*out|timeout|connection (?:refused|reset|aborted|closed)|(?:unable|failed) to connect|"
        r"network is unreachable|no route to host|name resolution|name or service not known|getaddrinfo|host not found|"
        r"(?:failed|unable) to resolve|SSL|TLS|certificate verify|proxyerror", message, re.I)
    host = (urlsplit(url).hostname or "").lower()
    domestic = any(host == domain or host.endswith("." + domain) for domain in (
        "bilibili.com", "bilibili.tv", "b23.tv", "xiaohongshu.com", "xhslink.com", "douyin.com", "iesdouyin.com"))
    if proxy and domestic and (network or (status and status[1] in {"403", "412", "429"})):
        hint = "请检查代理软件的分流规则，确认该站点使用合适的网络出口。"
    elif network:
        hint = "请检查「设置 → 网络代理」后重试。"
    else:
        return error
    return AppError(error.code, message + "\n" + hint, error.status)


# 按操作系统结束下载进程树，避免取消后遗留媒体子进程。
def terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


# 独立监控超时与停止信号，持续消费进程输出并在退出时回收进程树。
def run_process(args, timeout, on_line=None, stop=None, input_text=None):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    environment = {key: value for key, value in os.environ.items()
                   if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"}}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if input_text is not None:
        options["stdin"] = subprocess.PIPE
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", env=environment, **options)
    finished = threading.Event()
    timed_out = threading.Event()
    deadline = time.monotonic() + timeout

    def watch():
        while not finished.wait(0.1):
            if stop is not None and stop.is_set():
                terminate(process)
                return
            if time.monotonic() >= deadline:
                timed_out.set()
                terminate(process)
                return

    watcher = threading.Thread(target=watch, daemon=True, name="video-download-watch")
    watcher.start()
    lines = [] if on_line is None else deque(maxlen=80)
    try:
        if input_text is not None:
            try:
                process.stdin.write(input_text)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                # 继续读取子进程错误或由现有取消/超时分支解释原因。
                pass
        for line in process.stdout:
            if on_line is None:
                lines.append(line)
            elif not on_line(line.rstrip("\r\n")):
                lines.append(line[-2000:])
        process.wait()
        if stop is not None and stop.is_set():
            raise AppError("download_interrupted", "下载已中断，请重新下载。")
        if timed_out.is_set():
            raise AppError("video_download_timeout", "链接解析或视频下载超时，请稍后重试。")
        output = "".join(lines)
        if process.returncode:
            errors = [line.strip() for line in output.splitlines() if line.startswith("ERROR:")]
            message = (errors[-1] if errors else output.strip())[-2000:] or "下载器执行失败，请查看错误报告。"
            raise AppError("video_download_failed", message)
        return output
    finally:
        original_error = sys.exc_info()[0] is not None
        finished.set()
        try:
            try:
                terminate(process)
                process.wait(timeout=3)
            finally:
                process.stdout.close()
                if input_text is not None and not process.stdin.closed:
                    process.stdin.close()
        except (OSError, subprocess.TimeoutExpired):
            if not original_error:
                raise
            logger.warning("download process cleanup failed", exc_info=True)
        finally:
            watcher.join(timeout=3)
