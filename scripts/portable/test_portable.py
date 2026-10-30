"""Portable contracts. Run: python -m unittest discover -s scripts/portable -p test_*.py"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


launcher = module("launcher")
builder = module("build")


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="StudyBuddy 中文 空格 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_single_instance_lock_releases(self):
        first = launcher.InstanceLock(self.root)
        second = launcher.InstanceLock(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            if not first.file.closed:
                first.close()
            second.close()

    def test_prepare_failure_has_no_success_marker_and_retry_works(self):
        with patch.object(launcher.subprocess, "run", side_effect=RuntimeError("unpack failed")):
            with self.assertRaisesRegex(RuntimeError, "unpack failed"):
                launcher.prepare(self.root)
        self.assertFalse((self.root / ".runtime-ready.json").exists())
        with patch.object(launcher.subprocess, "run"), patch.object(launcher, "runtime_check", side_effect=RuntimeError("DLL")):
            with self.assertRaisesRegex(RuntimeError, "DLL"):
                launcher.prepare(self.root)
        self.assertFalse((self.root / ".runtime-ready.json").exists())
        with patch.object(launcher.subprocess, "run") as unpack, patch.object(launcher, "runtime_check"):
            launcher.prepare(self.root)
            launcher.prepare(self.root)
            self.assertEqual(unpack.call_count, 1)
        self.assertTrue((self.root / ".runtime-ready.json").exists())
        self.assertFalse((self.root / ".runtime-preparing.json").exists())

    def test_moved_prepared_and_partial_environments_are_rejected(self):
        for filename in (".runtime-ready.json", ".runtime-preparing.json"):
            path = self.root / filename
            path.write_text(json.dumps({"root": str(self.root / "old")}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "重新解压"):
                launcher.prepare(self.root)
            path.unlink()

    def test_isolated_unpack_can_import_sibling_and_receives_no_extra_arguments(self):
        directory = self.root / "runtime/Scripts"
        directory.mkdir(parents=True)
        (directory / "conda_unpack_progress.py").write_text("VALUE = 42\n", encoding="utf-8")
        (directory / "conda-unpack-script.py").write_text(
            "import argparse, conda_unpack_progress\nassert conda_unpack_progress.VALUE == 42\n"
            "argparse.ArgumentParser().parse_args()\n", encoding="utf-8")
        with patch.object(launcher.subprocess, "run") as unpack, patch.object(launcher, "runtime_check"):
            launcher.prepare(self.root)
            command = unpack.call_args.args[0]
        subprocess.run([sys.executable, *command[1:]], check=True)

    def test_write_failure_reports_nonzero(self):
        with patch.object(launcher, "InstanceLock", side_effect=PermissionError("read-only")):
            self.assertEqual(launcher.main(self.root), 1)

    def test_exclusive_bind_skips_busy_ports_without_stopping_them(self):
        connection = launcher.bind_port()
        self.addCleanup(connection.close)
        port = connection.getsockname()[1]
        self.assertTrue(launcher.owns_port(os.getpid(), port))
        other = launcher.bind_port()
        self.addCleanup(other.close)
        self.assertNotEqual(other.getsockname()[1], port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass

    def test_all_ports_busy_fails_without_touching_services(self):
        class Busy:
            def setsockopt(self, *args): pass
            def bind(self, *args): raise OSError("busy")
            def close(self): pass
        with patch.object(launcher.socket, "socket", return_value=Busy()):
            with self.assertRaisesRegex(RuntimeError, "均被占用"):
                launcher.bind_port()

    def test_stale_process_or_other_listener_never_opens_browser(self):
        state = {"ready": True, "root": str(self.root), "pid": os.getpid(), "port": 18000, "process_stamp": 123}
        launcher.atomic_json(self.root / ".studybuddy-instance.json", state)
        for stamp, owner in ((999, True), (123, False)):
            with patch.object(launcher, "process_stamp", return_value=stamp), patch.object(launcher, "owns_port", return_value=owner), patch.object(launcher, "healthy", return_value=True), patch.object(launcher, "open_page") as opened:
                launcher.existing_instance(self.root)
                opened.assert_not_called()
        with patch.object(launcher, "process_stamp", return_value=123), patch.object(launcher, "owns_port", return_value=True), patch.object(launcher, "healthy", return_value=True), patch.object(launcher, "open_page") as opened:
            launcher.existing_instance(self.root)
            opened.assert_called_once_with(18000)

    def test_browser_failure_keeps_manual_address(self):
        with patch.object(launcher.webbrowser, "open", side_effect=OSError("browser")):
            launcher.open_page(18000)

    def test_health_deadline_stops_startup_without_opening_page(self):
        class NeverReady:
            started = False
            should_exit = False
            def __init__(self, config): pass
            def run(self, sockets):
                for _ in range(300):
                    if self.should_exit:
                        return
                    threading.Event().wait(0.01)
                raise AssertionError("Readiness timer did not stop the server")
        fake = SimpleNamespace(Server=NeverReady, Config=lambda *args, **kwargs: None)
        with patch.dict(__import__("sys").modules, {"uvicorn": fake}), patch.object(launcher.time, "monotonic", side_effect=[0, 121]), patch.object(launcher, "open_page") as opened:
            with self.assertRaisesRegex(RuntimeError, "服务启动失败"):
                launcher.serve(self.root)
            opened.assert_not_called()
        self.assertFalse((self.root / ".studybuddy-instance.json").exists())

    def test_configuration_clears_developer_credentials_and_paths(self):
        (self.root / "runtime/Library/bin").mkdir(parents=True)
        original = list(__import__("sys").path)
        directory = Path.cwd()
        try:
            with patch.dict(os.environ, {"LLM_API_KEY": "private-test", "MODELSCOPE_TOKEN": "private-test", "ASR_DEVICE": "cuda", "PYTHONPATH": "developer"}):
                handles = launcher.configure(self.root)
                for handle in handles:
                    handle.close()
                self.assertNotIn("LLM_API_KEY", os.environ)
                self.assertNotIn("MODELSCOPE_TOKEN", os.environ)
                self.assertEqual(os.environ["ASR_DEVICE"], "cpu")
                self.assertNotIn("developer", os.environ["PATH"])
                self.assertEqual(os.environ["DATA_DIR"], str(self.root / "data"))
        finally:
            os.chdir(directory)
            __import__("sys").path[:] = original

    def test_allowlist_ignores_unrelated_tracked_and_untracked_files(self):
        inputs = "backend/app/main.py\0backend/app/__pycache__/bad.py\0backend/tests/test_api.py\0frontend/package-lock.json\0frontend/src/App.tsx\0frontend/tests/test.ts\0frontend/debug.log"
        with patch.object(builder, "git", return_value=inputs):
            before = builder.tracked_inputs(self.root)
            (self.root / "private-unrelated.txt").write_text("never collect", encoding="utf-8")
            after = builder.tracked_inputs(self.root)
        self.assertEqual(before, after)
        self.assertEqual(before, (["backend/app/main.py"], ["frontend/package-lock.json", "frontend/src/App.tsx"]))

    def test_audit_requires_tools_and_rejects_private_paths(self):
        required = ("启动.cmd", "launcher.py", "使用说明.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "runtime/python.exe", "runtime/Scripts/conda-unpack-script.py", "backend/app/main.py", "web/index.html", "tools/ffmpeg.exe", "tools/ffprobe.exe", "licenses/conda-explicit.txt", "docs/SOURCE.md")
        for name in required:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"required")
        builder.audit_package(self.root)
        for name in (".env", "data/private.json", "log/secret.log", "backend/tests/test.py", "runtime/private.pyc"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"forbidden")
            with self.assertRaisesRegex(RuntimeError, "package path|backend path|Cache/log"):
                builder.audit_package(self.root)
            destination.unlink()
            if name.startswith(("data/", "log/", "backend/tests/")):
                destination.parent.rmdir()


if __name__ == "__main__":
    unittest.main()
