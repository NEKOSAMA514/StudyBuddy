"""Run local acceptance against an independent ZIP extraction, never staging runtime."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def wait_until(condition, process=None, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        if process and process.poll() is not None:
            raise RuntimeError(f"Validation launcher exited: {process.returncode}")
        time.sleep(0.2)
    raise TimeoutError("Acceptance wait timed out")


def start(package, output, suffix):
    # Suppress automatic tabs during repeated acceptance launches only.
    code = ("import runpy,webbrowser; webbrowser.open=lambda url:False; "
            "m=runpy.run_path('launcher.py',run_name='portable_acceptance'); raise SystemExit(m['main']())")
    stdout = (output / (suffix + ".log")).open("w", encoding="utf-8")
    stderr = (output / (suffix + "-errors.log")).open("w", encoding="utf-8")
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    process = subprocess.Popen([str(package / "runtime/python.exe"), "-I", "-B", "-u", "-X", "utf8", "-c", code],
        cwd=package, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info)
    stdout.close()
    stderr.close()
    return process


def ctrl_c(process, python):
    if process.poll() is not None:
        return
    # A short helper attaches to the test's own hidden console, then emits the
    # same CTRL_C_EVENT as the user's keyboard. No signal reaches another service.
    code = ("import ctypes,sys,time; k=ctypes.windll.kernel32; k.FreeConsole(); "
            "assert k.AttachConsole(int(sys.argv[1])); k.SetConsoleCtrlHandler(None,True); "
            "assert k.GenerateConsoleCtrlEvent(0,0); time.sleep(0.5)")
    subprocess.run([str(python), "-I", "-c", code, str(process.pid)], check=True)
    process.wait(timeout=120)
    assert process.returncode == 0, process.returncode


def request(port, path, value=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    query = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
        data=json.dumps(value).encode() if value is not None else None,
        headers={"Content-Type": "application/json"}, method="PUT" if value is not None else "GET")
    with opener.open(query, timeout=5) as response:
        return json.load(response)


def local_capabilities(package, output, repo, speech_source):
    launcher = load(package / "launcher.py", "acceptance_launcher")
    handles = launcher.configure(package)
    try:
        launcher.runtime_check(package)
        from app.config import Settings
        from app.recognition.chinese_asr import ChineseASR
        from app.recognition.media import Recognizer
        from app.documents.extraction import extract
        from PIL import Image, ImageDraw, ImageFont
        settings = Settings()
        speech = output / "synthetic.wav"
        if speech_source.resolve() != speech:
            shutil.copyfile(speech_source, speech)
        segments = ChineseASR(settings).transcribe(speech)
        recognized = "".join(item["text"] for item in segments)
        assert recognized and any(word in recognized for word in ("学习", "三角", "数学")), recognized
        image = Image.new("RGB", (1100, 220), "white")
        draw = ImageDraw.Draw(image)
        draw.text((30, 50), "StudyBuddy 本地识别 12345", fill="black",
                  font=ImageFont.truetype(str(Path(os.environ["SystemRoot"]) / "Fonts/msyh.ttc"), 55))
        image_path = output / "synthetic.png"
        image.save(image_path)
        text_result, confidence = Recognizer(settings).ocr(image_path)
        assert "12345" in text_result and "StudyBuddy" in text_result, text_result
        video = output / "synthetic.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-loop", "1", "-i", str(image_path),
                        "-i", str(speech), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)
        media = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(video)]))
        assert float(media["format"]["duration"]) > 1
        sys.path.insert(0, str(repo / "backend"))
        from tests.document_samples import sample
        documents = {}
        for extension in ("pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"):
            path = output / ("synthetic." + extension)
            path.write_bytes(sample(extension))
            result = extract(path, extension)
            assert result["units"] and any(unit["text"].strip() for unit in result["units"]), extension
            documents[extension] = len(result["units"])
        tools = {name: shutil.which(name) for name in ("python", "ffmpeg", "ffprobe", "deno")}
        assert all(Path(path).resolve().is_relative_to(package) for path in tools.values())
        child = subprocess.check_output([sys.executable, "-I", "-c", "import sys,shutil,json; print(json.dumps([sys.executable,shutil.which('deno'),shutil.which('ffmpeg')]))"], text=True)
        assert all(Path(path).resolve().is_relative_to(package) for path in json.loads(child))
        return {"asr_text": recognized, "ocr_text": text_result, "ocr_confidence": confidence,
                "media_seconds": media["format"]["duration"], "documents": documents, "tools": tools,
                "child_tools": json.loads(child)}
    finally:
        for handle in handles:
            handle.close()


def main(args):
    package, output, repo = args.package.resolve(), args.output.resolve(), args.repo.resolve()
    speech_source = args.speech.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    summary = {"status": "failed", "clean_machine": False, "real_cloud_api": False, "checks": []}
    process = None
    busy = None
    try:
        manifest = json.loads((package / "release-manifest.json").read_text(encoding="utf-8"))
        for name, record in manifest["files"].items():
            assert digest(package / name) == record["sha256"], name
            assert (package / name).stat().st_size == record["size"], name
        assert not any((package / name).exists() for name in ("data", "log", ".runtime-ready.json"))
        summary["checks"].append("manifest-and-pristine-extraction")
        # A real failed initialization, then an unchanged-directory retry.
        unpack = package / "runtime/Scripts/conda-unpack-script.py"
        held = unpack.with_suffix(".held")
        unpack.rename(held)
        try:
            process = start(package, output, "prepare-failed")
            assert process.wait(timeout=30) != 0
            assert not (package / ".runtime-ready.json").exists()
        finally:
            held.rename(unpack)
        summary["checks"].append("real-prepare-failure-no-success-marker")
        # Hold the preferred port only if it is available; never disturb its owner.
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            busy.bind(("127.0.0.1", 18000))
            busy.listen()
        except OSError:
            busy.close()
            busy = None
        process = start(package, output, "first-start")
        wait_until(lambda: (package / ".runtime-preparing.json").exists(), process)
        duplicate = start(package, output, "duplicate-preparing")
        assert duplicate.wait(timeout=30) == 0
        state_path = package / ".studybuddy-instance.json"
        def ready():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))["ready"]
            except (OSError, ValueError):
                return False
        wait_until(ready, process, seconds=240)  # Includes first-time prefix repair.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["pid"] == process.pid and state["port"] != 18000
        health = request(state["port"], "/api/v1/health")
        assert health["status"] == "ok" and not health["model_configured"]
        duplicate = start(package, output, "duplicate-ready")
        assert duplicate.wait(timeout=30) == 0
        summary["checks"].extend(["chinese-space-path-first-run", "duplicate-preparing-and-ready", "busy-port-fallback", "empty-credentials"])
        summary["health"] = health
        summary["port"] = state["port"]
        saved = {"summary_prompt": "便携包隔离验收", "note_style": "concise"}
        request(state["port"], "/api/v1/settings/note-style", saved)
        ctrl_c(process, package / "runtime/python.exe")
        assert not state_path.exists()
        summary["checks"].append("actual-console-ctrl-c-and-lock-release")
        process = start(package, output, "restart")
        wait_until(ready, process)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        recovered = request(state["port"], "/api/v1/settings/note-style")
        assert recovered["note_style"] == "concise" and recovered["summary_prompt"] == saved["summary_prompt"]
        ctrl_c(process, package / "runtime/python.exe")
        summary["checks"].append("settings-save-and-restart-recovery")
        summary["capabilities"] = local_capabilities(package, output, repo, speech_source)
        summary["checks"].append("real-asr-ocr-ffmpeg-pdf-office-and-child-paths")
        summary["status"] = "passed"
    finally:
        if process and process.poll() is None:
            ctrl_c(process, package / "runtime/python.exe")
        if busy:
            busy.close()
        (output / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("package", "output", "repo", "speech"):
        parser.add_argument("--" + name, type=Path, required=True)
    main(parser.parse_args())
