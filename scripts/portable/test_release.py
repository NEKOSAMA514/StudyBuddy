"""Release boundaries, using tiny ZIPs and a fake GitHub CLI."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


spec = importlib.util.spec_from_file_location("portable_release", Path(__file__).with_name("release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
COMMIT = "a" * 40
VERSION = "1.0.1-beta.1"
REPOSITORY = "example/StudyBuddy"


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(release.os.environ, {"GITHUB_STEP_SUMMARY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "dist/release"
        self.assets.mkdir(parents=True)
        self.archive = self.assets / f"StudyBuddy-v{VERSION}-windows-x64-preview.zip"
        self.manifest = {"version": VERSION, "source": {"commit": COMMIT, "status": ""}}
        self.report = {"status": "passed", "mode": "smoke", "source_commit": COMMIT, "version": VERSION}
        self.write_zip()
        self.write_report()
        self.draft = {"draft": True, "target_commitish": COMMIT, "tag_name": "v" + VERSION,
                      "html_url": "https://example.com/draft"}

    def write_zip(self):
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("StudyBuddy-Windows-x64/release-manifest.json", json.dumps(self.manifest))
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        Path(str(self.archive) + ".sha256").write_text(f"{digest}  {self.archive.name}\n", encoding="ascii")

    def write_report(self):
        (self.assets / "acceptance.json").write_text(json.dumps(self.report), encoding="utf-8")

    def test_version_matches_builder_format(self):
        for value in ("1.0.0", "1.0.1-beta.1"):
            self.assertEqual(release.version_tag(value), "v" + value)
        for value in ("v1.0.0", "1.0", "1.0.0\n", "1.0.0;echo bad", "../1.0.0", " 1.0.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.version_tag(value)

    def test_cli_rejects_non_main_before_github_access(self):
        with patch.dict(release.os.environ, {"GITHUB_REF": "refs/heads/feature"}), \
                patch("sys.argv", ["release.py", "check"]), patch.object(release, "api") as api:
            with self.assertRaisesRegex(RuntimeError, "only be run from main"):
                release.main()
            api.assert_not_called()

    def test_api_only_treats_http_404_as_missing(self):
        response = subprocess.CompletedProcess([], 1, "HTTP/2.0 404 Not Found\n\n{}", "not found")
        with patch.object(release.subprocess, "run", return_value=response):
            self.assertIsNone(release.api("repos/example/repo"))
        for code in (401, 403, 500):
            response.stdout = f"HTTP/2.0 {code} Error\n\n{{}}"
            with patch.object(release.subprocess, "run", return_value=response), self.assertRaises(RuntimeError):
                release.api("repos/example/repo")
        response = subprocess.CompletedProcess([], 0, 'HTTP/2.0 200 OK\nX-Test: yes\n\n{"draft":true}', "")
        with patch.object(release.subprocess, "run", return_value=response):
            self.assertEqual(release.api("repos/example/repo"), {"draft": True})

    def test_check_allows_new_version_and_same_commit_draft(self):
        for value in (None, self.draft):
            with patch.object(release, "api", side_effect=[None, [value] if value else []]):
                self.assertEqual(release.check_release(REPOSITORY, VERSION, COMMIT), value)

    def test_pending_draft_is_found_in_paginated_release_list(self):
        first_page = [{**self.draft, "tag_name": "v0.0.0"}] * 100
        with patch.object(release, "api", side_effect=[None, first_page, [self.draft]]) as api:
            self.assertEqual(release.check_release(REPOSITORY, VERSION, COMMIT), self.draft)
            self.assertEqual(api.call_args_list[2].args[0], f"repos/{REPOSITORY}/releases?per_page=100&page=2")

    def test_check_rejects_published_or_different_commit_draft(self):
        for value in ({**self.draft, "draft": False}, {**self.draft, "target_commitish": "b" * 40}):
            with patch.object(release, "api", side_effect=[None, [value]]), self.assertRaises(RuntimeError):
                release.check_release(REPOSITORY, VERSION, COMMIT)

    def test_check_resolves_annotated_tag_and_rejects_wrong_target(self):
        tag = {"object": {"type": "tag", "sha": "c" * 40}}
        target = {"object": {"type": "commit", "sha": COMMIT}}
        with patch.object(release, "api", side_effect=[tag, target, [self.draft]]) as api:
            self.assertEqual(release.check_release(REPOSITORY, VERSION, COMMIT), self.draft)
            self.assertIn("/git/tags/", api.call_args_list[1].args[0])
        target["object"]["sha"] = "b" * 40
        with patch.object(release, "api", return_value=target), self.assertRaisesRegex(RuntimeError, "different commit"):
            release.check_release(REPOSITORY, VERSION, COMMIT)

    def test_assets_require_matching_hash_and_clean_source(self):
        self.archive.write_bytes(b"damaged download")
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            release.release_assets(self.assets, VERSION, COMMIT)
        for key, value in (("commit", "b" * 40), ("status", " M backend/app/main.py")):
            self.manifest["source"] = {"commit": COMMIT, "status": "", key: value}
            self.write_zip()
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "clean source commit"):
                release.release_assets(self.assets, VERSION, COMMIT)

    def test_failed_or_unrelated_report_prevents_github_writes(self):
        for field, value in (("status", "failed"), ("source_commit", "b" * 40),
                             ("version", "9.0.0"), ("mode", "full")):
            original = dict(self.report)
            self.report[field] = value
            self.write_report()
            with self.subTest(field=field), patch.object(release.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "passing smoke report"):
                    release.create_draft(REPOSITORY, VERSION, COMMIT, self.assets, "https://example.com/run")
                run.assert_not_called()
            self.report = original

    def test_assets_at_size_limit_are_rejected(self):
        with patch.object(release, "ASSET_LIMIT", self.archive.stat().st_size):
            with self.assertRaisesRegex(RuntimeError, "smaller than 2 GiB"):
                release.release_assets(self.assets, VERSION, COMMIT)

    def test_stage_selects_only_requested_attachments(self):
        for path in (self.archive, Path(str(self.archive) + ".sha256")):
            path.rename(self.root / "dist" / path.name)
        output = self.root / "dist/cd-validation"
        output.mkdir()
        (self.assets / "acceptance.json").rename(output / "acceptance.json")
        (self.root / "dist/ffmpeg-sources.zip").write_bytes(b"build-only input")
        release.stage(self.root, VERSION, COMMIT)
        self.assertEqual({path.name for path in self.assets.iterdir()},
                         {self.archive.name, self.archive.name + ".sha256", "acceptance.json"})

    def test_create_leaves_draft_and_attaches_only_three_files(self):
        with patch.object(release, "api", side_effect=[None, [], None, [self.draft]]), \
                patch.object(release.subprocess, "run") as run:
            release.create_draft(REPOSITORY, VERSION, COMMIT, self.assets, "https://example.com/run")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "release", "create"])
        self.assertIn("--draft", command)
        self.assertIn("--prerelease", command)
        self.assertEqual(command[command.index("--target") + 1], COMMIT)
        self.assertEqual(command[4:7], list(map(str, release.release_assets(self.assets, VERSION, COMMIT))))
        self.assertNotIn("ffmpeg-sources.zip", " ".join(command))
        self.assertIn(COMMIT, (self.root / "dist/release-notes.md").read_text(encoding="utf-8"))

    def test_retry_updates_same_commit_draft_without_replacing_notes(self):
        with patch.object(release, "api", side_effect=[None, [self.draft], None, [self.draft]]), \
                patch.object(release.subprocess, "run") as run:
            release.create_draft(REPOSITORY, VERSION, COMMIT, self.assets, "https://example.com/run")
        edit, upload = (call.args[0] for call in run.call_args_list)
        self.assertIn("--draft", edit)
        self.assertNotIn("--notes-file", edit)
        self.assertEqual(upload[:3], ["gh", "release", "upload"])
        self.assertIn("--clobber", upload)

    def test_published_version_is_never_edited(self):
        with patch.object(release, "api", side_effect=[None, [{**self.draft, "draft": False}]]), \
                patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "already published"):
                release.create_draft(REPOSITORY, VERSION, COMMIT, self.assets, "https://example.com/run")
            run.assert_not_called()

    def test_upload_error_fails_without_publishing(self):
        with patch.object(release, "api", side_effect=[None, []]), \
                patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "gh")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                release.create_draft(REPOSITORY, VERSION, COMMIT, self.assets, "https://example.com/run")
            self.assertEqual(run.call_count, 1)
            self.assertIn("--draft", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
