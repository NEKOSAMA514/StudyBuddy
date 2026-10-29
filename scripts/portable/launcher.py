"""Windows portable entry point. No application imports before environment setup."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser


ROOT = Path(__file__).resolve().parent


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class InstanceLock:
    def __init__(self, root):
        self.file = (root / ".studybuddy.lock").open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.held = False

    def acquire(self):
        import msvcrt
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            self.held = True
        except OSError:
            pass
        return self.held

    def close(self):
        if self.held:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def owns_port(pid, port):
    """Check the kernel TCP table, so stale state cannot open another service."""
    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "state", "address", "port", "remote_address", "remote_port", "pid")]
    size = wintypes.DWORD()
    get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    get_table(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0):
        return False
    count = wintypes.DWORD.from_buffer(buffer).value
    for index in range(count):
        row = Row.from_buffer(buffer, 4 + ctypes.sizeof(Row) * index)
        if (row.pid == pid and row.state == 2 and socket.ntohs(row.port & 65535) == port
                and row.address == int.from_bytes(socket.inet_aton("127.0.0.1"), "little")):
            return True
    return False


def process_stamp(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def healthy(port):
    try:
        # Never route a loopback health check through a machine proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def open_page(port):
    address = f"http://127.0.0.1:{port}"
    print(f"StudyBuddy: {address}  (Ctrl+C 正常停止)", flush=True)
    try:
        opened = webbrowser.open(address)
    except Exception:
        opened = False
    if not opened:
        print("浏览器未能自动打开，请手动打开上面的地址。", flush=True)


def existing_instance(root):
    try:
        state = json.loads((root / ".studybuddy-instance.json").read_text(encoding="utf-8"))
        port, pid = state["port"], state["pid"]
        if (state["ready"] and state["root"] == str(root)
                and process_stamp(pid) == state["process_stamp"] and owns_port(pid, port)
                and healthy(port)):
            open_page(port)
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print("当前目录已有实例正在准备或退出，请等待原窗口完成。", flush=True)


def configure(root):
    # Remove developer shell defaults; persisted user settings are loaded by the app.
    names = ("MAX_UPLOAD_MB MAX_DURATION_SECONDS FRAME_INTERVAL_SECONDS ASR_MODEL ASR_ENGINE "
             "ASR_LANGUAGE ASR_THREADS ASR_DEVICE ASR_COMPUTE_TYPE OCR_ENGINE OCR_MIN_CONFIDENCE "
             "LLM_PROVIDER LLM_BASE_URL LLM_MODEL MODELSCOPE_TOKEN LLM_API_KEY LLM_STREAM "
             "LLM_JSON_MODE LLM_SECTION_CONCURRENCY LLM_FREE_TIER_CONFIRMED PYTHONHOME PYTHONPATH "
             "HF_HUB_CACHE HUGGINGFACE_HUB_CACHE HF_XET_CACHE TRANSFORMERS_CACHE HF_ENDPOINT "
             "HF_TOKEN HUGGING_FACE_HUB_TOKEN HF_TOKEN_PATH HF_HUB_OFFLINE")
    for name in names.split():
        os.environ.pop(name, None)
    runtime = root / "runtime"
    system = Path(os.environ["SystemRoot"])
    os.environ.update(
        PATH=os.pathsep.join(str(path) for path in (
            runtime, runtime / "Scripts", runtime / "Library/bin", root / "tools",
            system / "System32", system)),
        DATA_DIR=str(root / "data"), LOG_DIR=str(root / "log"), MODEL_DIR=str(root / "models"),
        STATIC_DIR=str(root / "web"), HF_HOME=str(root / "models/_cache"),
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1", ASR_ENGINE="paraformer", ASR_DEVICE="cpu",
        ASR_COMPUTE_TYPE="int8", OCR_ENGINE="rapidocr", CONDA_PREFIX=str(runtime),
        TEMP=str(root / "data/_tmp"), TMP=str(root / "data/_tmp"),
        DENO_DIR=str(root / "data/_cache/deno"), XDG_CACHE_HOME=str(root / "data/_cache"))
    sys.dont_write_bytecode = True
    os.chdir(root)
    sys.path.insert(0, str(root / "backend"))
    # Keep native DLL lookup within the bundle, including on Python 3.8+.
    return [os.add_dll_directory(str(path)) for path in (runtime, runtime / "Library/bin")]


def prepare(root):
    marker = root / ".runtime-ready.json"
    journal = root / ".runtime-preparing.json"
    for path in (marker, journal):
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if os.path.normcase(previous["root"]) != os.path.normcase(str(root)):
                raise RuntimeError("已准备的环境被移动。请在固定目录重新解压原始 ZIP，再迁移 data；不要修补旧环境。")
    for name in ("data", "log", "data/_tmp"):
        directory = root / name
        directory.mkdir(exist_ok=True)
        probe = directory / (".write-" + uuid.uuid4().hex)
        probe.write_bytes(b"")
        probe.unlink()
    if marker.exists():
        return
    print("首次准备独立运行环境，请保持目录固定并等待……", flush=True)
    atomic_json(journal, {"root": str(root)})
    unpack_code = ("import pathlib,runpy,sys; script=sys.argv[1]; sys.argv=[script]; "
                   "sys.path.insert(0,str(pathlib.Path(script).parent)); runpy.run_path(script,run_name='__main__')")
    subprocess.run([str(root / "runtime/python.exe"), "-I", "-B", "-X", "utf8", "-c", unpack_code,
                    str(root / "runtime/Scripts/conda-unpack-script.py")], check=True)
    runtime_check(root)
    atomic_json(marker, {"root": str(root), "prepared_at": time.time()})
    journal.unlink(missing_ok=True)


def runtime_check(root):
    code = """import importlib.metadata as m, pathlib, shutil, subprocess, sys
import av, cryptography, ctranslate2, cv2, deno, fastapi, numpy, onnxruntime, pypdfium2, rapidocr, sherpa_onnx, sqlalchemy, yt_dlp_ejs
from yt_dlp import YoutubeDL
assert sys.version_info[:3] == (3, 12, 15)
assert not any(d.metadata['Name'].lower().startswith('pymupdf') for d in m.distributions())
for name in ('ffmpeg', 'ffprobe', 'deno'):
    path = pathlib.Path(shutil.which(name)).resolve()
    assert path.is_relative_to(pathlib.Path(sys.argv[1])), (name, path)
    subprocess.run([str(path), '--version' if name == 'deno' else '-version'], check=True, stdout=subprocess.DEVNULL)
subprocess.run([sys.executable, '-I', '-m', 'yt_dlp', '--ignore-config', '--version'], check=True)
with YoutubeDL({'quiet': True, 'cachedir': False}) as downloader:
    info = downloader._js_runtimes['deno'].info
    assert info and info.supported and pathlib.Path(shutil.which(info.path)).resolve().is_relative_to(pathlib.Path(sys.argv[1]))
for name in ('core.min.js', 'lib.min.js'):
    assert (pathlib.Path(yt_dlp_ejs.__file__).parent / 'yt/solver' / name).is_file()
"""
    subprocess.run([str(root / "runtime/python.exe"), "-I", "-B", "-c", code, str(root)], check=True)


def bind_port():
    for port in range(18000, 18020):
        connection = socket.socket()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            connection.bind(("127.0.0.1", port))
            connection.listen(128)
            connection.setblocking(False)
            return connection
        except OSError:
            connection.close()
    raise RuntimeError("18000—18019 均被占用，请关闭不需要的服务后重试。现有服务未被停止。")


def serve(root):
    import uvicorn
    connection = bind_port()
    port = connection.getsockname()[1]
    state_file = root / ".studybuddy-instance.json"
    state = {"pid": os.getpid(), "process_stamp": process_stamp(os.getpid()),
             "port": port, "root": str(root), "ready": False}
    atomic_json(state_file, state)
    stopped = threading.Event()
    ready = threading.Event()
    # The Windows default Proactor loop stalled at shutdown after browser streams.
    server = uvicorn.Server(uvicorn.Config("app.main:create_app", factory=True, host="127.0.0.1", port=port,
                                         workers=1, log_level="info", loop="asyncio:SelectorEventLoop"))

    def watch():
        deadline = time.monotonic() + 120
        while not stopped.wait(0.2):
            if server.started and owns_port(os.getpid(), port) and healthy(port):
                state["ready"] = True
                atomic_json(state_file, state)
                ready.set()
                open_page(port)
                return
            if time.monotonic() >= deadline:
                print(f"服务在 120 秒内未就绪。日志：{root / 'log'}", flush=True)
                server.should_exit = True
                return
    watcher = threading.Thread(target=watch, name="portable-readiness", daemon=True)
    watcher.start()
    try:
        server.run(sockets=[connection])
        if not ready.is_set():
            raise RuntimeError("服务启动失败，请查看控制台及 log 目录。")
    except KeyboardInterrupt:
        print("StudyBuddy 已完成正常退出。", flush=True)
    finally:
        stopped.set()
        watcher.join(2)
        connection.close()
        state_file.unlink(missing_ok=True)


def main(root=ROOT):
    lock = None
    dll_handles = []
    try:
        if os.name != "nt":
            raise RuntimeError("此包仅支持 Windows 10/11 x64。")
        lock = InstanceLock(root)
        if not lock.acquire():
            existing_instance(root)
            return 0
        dll_handles = configure(root)
        prepare(root)
        serve(root)
        return 0
    except KeyboardInterrupt:
        print("准备已取消，下次可在相同目录重试。", flush=True)
        return 130
    except Exception as exc:
        print(f"启动失败：{exc}\n请确认已完整解压到可写目录。日志位置：{root / 'log'}", file=sys.stderr, flush=True)
        return 1
    finally:
        for handle in dll_handles:
            handle.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
