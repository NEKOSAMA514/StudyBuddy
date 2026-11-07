"""Acceptance mode selection and failed startup reporting, without cloud services."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("portable_validate", Path(__file__).with_name("validate.py"))
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.output = self.root / "report"
        self.arguments = ["--package", str(self.package), "--output", str(self.output), "--repo", str(self.root)]

    def test_smoke_does_not_require_speech_and_full_still_does(self):
        args = validate.parse_args([*self.arguments, "--smoke-only"])
        self.assertTrue(args.smoke_only)
        self.assertIsNone(args.speech)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            validate.parse_args(self.arguments)
        self.assertEqual(error.exception.code, 2)
        args = validate.parse_args([*self.arguments, "--speech", str(self.root / "speech.wav")])
        self.assertFalse(args.smoke_only)
        self.assertEqual(args.speech.name, "speech.wav")

    def test_startup_failure_records_mode_source_and_skipped_capabilities(self):
        manifest = {"source": {"commit": "a" * 40}, "version": "1.0.1", "files": {}}
        (self.package / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        unpack = self.package / "runtime/Scripts/conda-unpack-script.py"
        unpack.parent.mkdir(parents=True)
        unpack.write_text("# test", encoding="ascii")
        with patch.object(validate, "start", side_effect=RuntimeError("launcher failed")), \
                patch.object(validate, "local_capabilities") as capabilities:
            with self.assertRaisesRegex(RuntimeError, "launcher failed"):
                validate.main(validate.parse_args([*self.arguments, "--smoke-only"]))
            capabilities.assert_not_called()
        report = json.loads((self.output / "acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["mode"], "smoke")
        self.assertEqual(report["source_commit"], manifest["source"]["commit"])
        self.assertEqual(report["error"], "launcher failed")
        self.assertTrue({"asr", "ocr", "document-extraction", "real-cloud-api"}.issubset(report["skipped"]))
        self.assertTrue(unpack.is_file())

    def test_corrupt_manifest_file_exits_nonzero_and_saves_failed_report(self):
        (self.package / "example.txt").write_text("corrupt", encoding="ascii")
        manifest = {"source": {"commit": "a" * 40}, "version": "1.0.1",
                    "files": {"example.txt": {"sha256": "0" * 64, "size": 7}}}
        (self.package / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(validate.__file__)),
                                 *self.arguments, "--smoke-only"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((self.output / "acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertIn("example.txt", report["error"])

    def test_cleanup_failure_keeps_original_startup_error(self):
        manifest = {"source": {"commit": "a" * 40}, "version": "1.0.1", "files": {}}
        (self.package / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        unpack = self.package / "runtime/Scripts/conda-unpack-script.py"
        unpack.parent.mkdir(parents=True)
        unpack.write_text("# test", encoding="ascii")
        process = SimpleNamespace(wait=Mock(side_effect=RuntimeError("prepare crashed")), poll=Mock(return_value=None))
        with patch.object(validate, "start", return_value=process), \
                patch.object(validate, "ctrl_c", side_effect=RuntimeError("stop failed")):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                validate.main(validate.parse_args([*self.arguments, "--smoke-only"]))
        report = json.loads((self.output / "acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"], "prepare crashed")
        self.assertEqual(report["cleanup_error"], "stop failed")


if __name__ == "__main__":
    unittest.main()
