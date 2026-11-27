"""Check portable release inputs and create an unpublished GitHub release draft."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote
import zipfile


ROOT = Path(__file__).resolve().parents[2]
ASSET_LIMIT = 2 * 1024 ** 3


def version_tag(version):
    # Keep the format accepted by scripts/build-portable.ps1.
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid version; use e.g. 1.0.1-beta.1 without the v prefix")
    return "v" + version


def api(route):
    result = subprocess.run(["gh", "api", "--include", route], capture_output=True, text=True, encoding="utf-8")
    headers, _, body = result.stdout.partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d+)", headers)
    if status and status[1] == "404":
        return None
    if result.returncode or not status or status[1] != "200":
        raise RuntimeError("GitHub API request failed: " + (result.stderr or result.stdout).strip())
    return json.loads(body)


def check_release(repository, version, commit):
    tag = quote(version_tag(version), safe="")
    reference = api(f"repos/{repository}/git/ref/tags/{tag}")
    tagged_commit = None
    if reference:
        target = reference["object"]
        while target["type"] == "tag":
            target = api(f"repos/{repository}/git/tags/{target['sha']}")["object"]
        tagged_commit = target["sha"]
        if target["type"] != "commit" or tagged_commit != commit:
            raise RuntimeError("The version tag already points to a different commit")
    # The tag endpoint only finds published releases; list also includes drafts.
    page = 1
    while True:
        releases = api(f"repos/{repository}/releases?per_page=100&page={page}")
        if releases is None:
            raise RuntimeError("Cannot read the repository releases")
        release = next((item for item in releases if item["tag_name"] == tag), None)
        if release or len(releases) < 100:
            break
        page += 1
    if release:
        if not release["draft"]:
            raise RuntimeError("This version is already published; choose a new version")
        if (tagged_commit or release["target_commitish"]) != commit:
            raise RuntimeError("The existing draft belongs to a different commit")
    return release


def verify_zip(directory, version, commit):
    archive = directory / f"StudyBuddy-{version_tag(version)}-windows-x64.zip"
    for path in (archive, Path(str(archive) + ".sha256")):
        if path.stat().st_size >= ASSET_LIMIT:
            raise RuntimeError(f"Release asset must be smaller than 2 GiB: {path.name}")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    expected = f"{digest}  {archive.name}"
    if Path(str(archive) + ".sha256").read_text(encoding="ascii").strip() != expected:
        raise RuntimeError("Portable ZIP SHA-256 mismatch")
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("StudyBuddy-Windows-x64/release-manifest.json"))
    if manifest["version"] != version or manifest["source"]["commit"] != commit or manifest["source"]["status"]:
        raise RuntimeError("Portable ZIP does not match the version and clean source commit")
    return archive


def release_assets(directory, version, commit):
    archive = verify_zip(directory, version, commit)
    report = directory / "acceptance.json"
    if report.stat().st_size >= ASSET_LIMIT:
        raise RuntimeError("Release asset must be smaller than 2 GiB: acceptance.json")
    acceptance = json.loads(report.read_text(encoding="utf-8"))
    if (acceptance["status"] != "passed" or acceptance["mode"] != "smoke"
            or acceptance["source_commit"] != commit or acceptance["version"] != version):
        raise RuntimeError("A passing smoke report for this version and commit is required")
    return [archive, Path(str(archive) + ".sha256"), report]


def stage(root, version, commit):
    archive = verify_zip(root / "dist", version, commit)
    destination = root / "dist/release"
    destination.mkdir(parents=True, exist_ok=True)
    for source in (archive, Path(str(archive) + ".sha256"), root / "dist/cd-validation/acceptance.json"):
        shutil.copyfile(source, destination / source.name)
    release_assets(destination, version, commit)


def create_draft(repository, version, commit, directory, run_url):
    assets = release_assets(directory, version, commit)
    existing = check_release(repository, version, commit)
    tag = version_tag(version)
    if existing:
        subprocess.run(["gh", "release", "edit", tag, "--repo", repository,
                        "--draft", "--prerelease", "--latest=false", "--target", commit], check=True)
        subprocess.run(["gh", "release", "upload", tag, *map(str, assets),
                        "--repo", repository, "--clobber"], check=True)
    else:
        notes = directory.parent / "release-notes.md"
        notes.write_text(
            f"Windows x64 CPU 便携预览版。\n\n源码提交：`{commit}`\n\n[构建与验收记录]({run_url})\n\n"
            "完整解压 ZIP 到固定、可写目录，双击 `启动.cmd`。SHA-256 文件用于核对下载完整性。\n\n"
            "自动启动验收见 `acceptance.json`；发布前请手动试用视频与文档笔记、思维导图、语音、OCR 和真实 API。"
            "本次未完成 Windows 10/11 干净系统及完整业务验收。\n\n"
            "这是未公开的草稿，可编辑说明、增删附件，试用后再手动发布。\n",
            encoding="utf-8")
        subprocess.run(["gh", "release", "create", tag, *map(str, assets), "--repo", repository,
                        "--draft", "--prerelease", "--latest=false", "--target", commit,
                        "--title", f"StudyBuddy {tag}（预览版）", "--notes-file", str(notes)], check=True)
    release = check_release(repository, version, commit)
    if not release:
        raise RuntimeError("Release draft was not found after uploading")
    summary = f"已准备 [{tag} 草稿]({release['html_url']})，源码提交 `{commit}`。请下载试用后手动发布。\n"
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "verify", "stage", "draft"))
    args = parser.parse_args()
    if os.environ["GITHUB_REF"] != "refs/heads/main":
        raise RuntimeError("CD can only be run from main")
    version, commit = os.environ["RELEASE_VERSION"], os.environ["GITHUB_SHA"]
    version_tag(version)
    if args.command == "check":
        check_release(os.environ["GITHUB_REPOSITORY"], version, commit)
    elif args.command == "verify":
        print(verify_zip(ROOT / "dist", version, commit))
    elif args.command == "stage":
        stage(ROOT, version, commit)
    else:
        repository = os.environ["GITHUB_REPOSITORY"]
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        create_draft(repository, version, commit, ROOT / "dist/release", run_url)


if __name__ == "__main__":
    main()
