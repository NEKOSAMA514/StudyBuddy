"""Explicit-input portable assembly, run only in the dedicated build environment."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile


FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"
FFMPEG_HASH = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
LICENSE_ASSETS = Path(__file__).with_name("licenses")
PUBLICATION_TODOS = [
    "Windows 10 和 Windows 11 x64 干净系统验收尚未完成。",
    "真实云端模型和 MinerU API 验收尚未完成。",
    "独立 FFmpeg 及 PyAV 所带 FFmpeg 的完整构建材料尚未全部取得，现有源码出处见 THIRD_PARTY_NOTICES.txt。",
    "正式包必须从已提交源码重建，并确认 GitHub 单附件小于 2 GiB。",
]


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(args, **kwargs):
    print(">>> " + str(args[0]), flush=True)
    return subprocess.run([str(value) for value in args], check=True, **kwargs)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], encoding="utf-8").strip()


def tracked_inputs(root):
    files = git(root, "ls-files", "-z", "--", "backend/app", "frontend").split("\0")
    backend = []
    frontend = []
    for name in files:
        path = PurePosixPath(name)
        if name.startswith("backend/app/") and path.suffix == ".py" and "__pycache__" not in path.parts:
            backend.append(name)
        elif (name.startswith(("frontend/src/", "frontend/public/")) or name in {
            "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
            "frontend/tsconfig.json", "frontend/vite.config.ts"}):
            frontend.append(name)
    if "backend/app/main.py" not in backend or "frontend/package-lock.json" not in frontend:
        raise RuntimeError("Missing tracked application/build sources")
    return sorted(backend), sorted(frontend)


def copy_file(source, destination):
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"Expected a regular build input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def download(url, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def collect_ffmpeg(root, work, package):
    local = root / "tools/ffmpeg/ffmpeg-9.0.2-essentials_build.zip"
    archive = local if local.is_file() and sha256(local) == FFMPEG_HASH else work / "downloads/ffmpeg.zip"
    if archive != local:
        download(FFMPEG_URL, archive)
    if sha256(archive) != FFMPEG_HASH:
        raise RuntimeError("FFmpeg archive SHA-256 mismatch")
    with zipfile.ZipFile(archive) as source:
        for name in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt"):
            entry = "ffmpeg-9.0.2-essentials_build/" + name
            destination = package / ("tools/" + Path(name).name if name.startswith("bin/") else "licenses/ffmpeg/" + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as stream, destination.open("wb") as output:
                shutil.copyfileobj(stream, output)
    # Preserve the pinned upstream source referenced by this binary's README.
    readme = (package / "licenses/ffmpeg/README.txt").read_text(encoding="utf-8")
    revision = re.search(r"https://github.com/FFmpeg/FFmpeg/commit/([a-f0-9]+)", readme)[1]
    source_url = f"https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/{revision}"
    source_archive = package / "licenses/ffmpeg/upstream-source.tar.gz"
    download(source_url, source_archive)
    return {"version": "9.0.2-essentials", "url": FFMPEG_URL, "sha256": FFMPEG_HASH,
            "upstream_source": source_url, "source_sha256": sha256(source_archive),
            "complete_corresponding_source_review": "pending"}


def collect_models(root, package):
    sys.path.insert(0, str(package / "backend"))
    from app.recognition.model_catalog import MODELS
    from app.recognition.model_files import verify_paraformer
    import rapidocr
    results = {}
    for identifier in ("paraformer", "rapidocr"):
        spec = MODELS[identifier]
        target = package / "models" / spec["directory"]
        target.mkdir(parents=True)
        for name in spec["files"]:
            local = root / "models" / spec["directory"] / name
            destination = target / name
            if identifier == "rapidocr":
                copy_file(Path(rapidocr.__file__).parent / "models" / name, destination)
            elif local.is_file():
                copy_file(local, destination)
            else:
                download(f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{name}", destination)
        if identifier == "paraformer":
            verify_paraformer(target)
            base = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}"
            for name in ("README.md",):
                download(base + "/" + name, package / "licenses/models/paraformer" / name)
            results[identifier] = {"repo": spec["repo"], "revision": spec["revision"], "source": base}
        else:
            results[identifier] = {"distribution": "rapidocr", "version": metadata.version("rapidocr"),
                                   "source": "https://github.com/RapidAI/RapidOCR"}
        results[identifier]["files"] = {name: sha256(target / name) for name in spec["files"]}
    copy_file(LICENSE_ASSETS / "flatbuffers.LICENSE", package / "licenses/models/LICENSE-APACHE-2.0.txt")
    distribution = metadata.distribution("rapidocr")
    notice = next(entry for entry in distribution.files or [] if PurePosixPath(str(entry)).name == "MODEL_LICENSES.md")
    copy_file(Path(distribution.locate_file(notice)), package / "licenses/models/rapidocr/MODEL_LICENSES.md")
    return results


def collect_python_licenses(package):
    supplements = json.loads((LICENSE_ASSETS / "sources.json").read_text(encoding="utf-8"))
    distributions = []
    for distribution in sorted(metadata.distributions(), key=lambda value: value.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        if name.lower().startswith("pymupdf"):
            raise RuntimeError("PyMuPDF is prohibited in the runtime")
        directory = package / "licenses/python" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "METADATA.txt").write_text(distribution.read_text("METADATA") or "", encoding="utf-8")
        found = []
        for entry in distribution.files or []:
            path = PurePosixPath(str(entry).replace("\\", "/"))
            if ".." in path.parts or path.is_absolute() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if any(re.match(r"^(licen[sc]e|copying|notice|authors|copyright|third[-_ ]?party)", part, re.I) for part in path.parts):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    copy_file(source, directory / path)
                    found.append(str(path))
        # 补齐已确认没有自带许可文件的发行包，限定版本并核对上游原文哈希。
        supplement = None
        key = re.sub(r"[-_.]+", "-", name).lower()
        candidate = supplements.get(key)
        if not found and candidate and candidate["version"] == distribution.version:
            supplement = candidate
            payload = (LICENSE_ASSETS / supplement["file"]).read_text(encoding="utf-8").encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != supplement["sha256"]:
                raise RuntimeError(f"Supplemental license SHA-256 mismatch: {name}")
            (directory / "LICENSE").write_bytes(payload)
            found.append("LICENSE")
        distributions.append({"name": name, "version": distribution.version,
                              "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                              "project_urls": distribution.metadata.get_all("Project-URL", []),
                              "license_files": found, "supplemental_license": supplement,
                              "review": "license files collected" if found else "license file missing"})
    return distributions


def collect_conda_licenses(work, package):
    records = []
    for path in sorted((work / "env/conda-meta").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        identifier = f"{record['name']}-{record['version']}-{record['build']}"
        source = work / "pkgs" / identifier / "info"
        destination = package / "licenses/conda" / identifier
        destination.mkdir(parents=True)
        for filename in ("index.json", "about.json", "recipe/meta.yaml", "recipe/conda_build_config.yaml"):
            if (source / filename).is_file():
                copy_file(source / filename, destination / filename)
        if (source / "licenses").is_dir():
            shutil.copytree(source / "licenses", destination / "licenses")
        records.append({key: record.get(key) for key in ("name", "version", "build", "build_number", "channel", "url", "sha256", "md5", "license")})
    copy_file(work / "conda-explicit.txt", package / "licenses/conda-explicit.txt")
    return records


def third_party_notices():
    return ("StudyBuddy — 第三方声明\n\n项目自有代码：GPLv3（GPL-3.0-only），Copyright (c) 2026 NEKOSAMA。\n"
            "第三方软件和模型保留各自许可，本页列出随包材料位置。\n"
            "Python、Conda 和前端依赖的许可证、版权与来源分别位于 licenses/python、conda、frontend；准确版本和哈希见 release-manifest.json。\n"
            "模型的权利归属、固定来源和哈希对应的授权说明见 licenses/models。StudyBuddy 原样复制权重，没有训练或修改。\n"
            "独立 FFmpeg 来自 Gyan 9.0.2 GPLv3 构建；PyAV 所带的 FFmpeg、Shapely 所带 GEOS 及其他组件按其各自许可处理。\n"
            "独立 FFmpeg 的原始许可证、构建 README 和本体源码见 licenses/ffmpeg。\n"
            "PyAV 对应源码与构建出处：https://github.com/PyAV-Org/PyAV/tree/v15.1.0 及 https://github.com/PyAV-Org/pyav-ffmpeg/tree/7.1.1-6 。\n"
            "GEOS 3.14.1 对应源码：https://download.osgeo.org/geos/geos-3.14.1.tar.bz2 。\n"
            "上述源码位置不表示两套 FFmpeg 所有外部依赖的完整构建材料已经取得；独立工具的上游请求渠道见 https://github.com/GyanD/codexffmpeg/issues/249 。\n\n"
            "尚未完成事项：\n" + "\n".join("- " + item for item in PUBLICATION_TODOS) + "\n")


def collect_frontend_licenses(frontend, package):
    lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
    records = []
    # Include build dependencies too: this safely over-includes notices for bundled code.
    for name, record in sorted(lock["packages"].items()):
        if not name:
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not name.startswith("node_modules/"):
            raise RuntimeError("Unsafe package-lock path")
        folder = frontend / path
        found = []
        for source in folder.iterdir() if folder.is_dir() else []:
            if source.is_file() and re.match(r"^(licen[sc]e|copying|notice|copyright)", source.name, re.I):
                copy_file(source, package / "licenses/frontend" / path / source.name)
                found.append(source.name)
        records.append({"package": name, "version": record.get("version"), "source": record.get("resolved"),
                        "integrity": record.get("integrity"), "license": record.get("license"), "license_files": found})
    return records


def audit_package(package):
    required = ("启动.cmd", "launcher.py", "使用说明.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt",
                "runtime/python.exe", "runtime/Scripts/conda-unpack-script.py", "backend/app/main.py",
                "web/index.html", "tools/ffmpeg.exe", "tools/ffprobe.exe", "licenses/conda-explicit.txt")
    for name in required:
        if not (package / name).is_file():
            raise RuntimeError(f"Missing portable file: {name}")
    top = {"启动.cmd", "launcher.py", "使用说明.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "release-manifest.json",
           "runtime", "backend", "web", "tools", "models", "licenses"}
    prohibited = {".git", ".env", "__pycache__", ".pytest_cache", ".idea", "test-results", "playwright-report"}
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if path.is_symlink() or relative.parts[0] not in top:
            raise RuntimeError(f"Unexpected package path: {relative}")
        if any(part in prohibited or part.startswith(".env.") or part.lower().startswith("pymupdf") for part in relative.parts):
            raise RuntimeError(f"Prohibited package path: {relative}")
        if relative.parts[0] == "backend" and len(relative.parts) > 1 and relative.parts[1] != "app":
            raise RuntimeError(f"Non-business backend path: {relative}")
        if path.is_file() and path.suffix.lower() in {".pyc", ".pyo", ".log"}:
            raise RuntimeError(f"Cache/log in package: {relative}")


def main(args):
    root, work = args.root.resolve(), args.work.resolve()
    if not work.is_relative_to(root / "dist/.build") or work == root / "dist/.build":
        raise RuntimeError("Build work directory must be a unique child of dist/.build")
    package = work / "StudyBuddy-Windows-x64"
    package.mkdir()
    backend, frontend_files = tracked_inputs(root)
    source_files = backend + frontend_files + ["backend/requirements.lock", "LICENSE", "scripts/build-portable.ps1",
        "scripts/portable/build.py", "scripts/portable/launcher.py", "scripts/portable/启动.cmd", "scripts/portable/使用说明.txt"]
    source_files += [str(path.relative_to(root)).replace("\\", "/") for path in sorted(LICENSE_ASSETS.iterdir()) if path.is_file()]
    input_hashes = {name: sha256(root / name) for name in source_files}
    for name in backend:
        copy_file(root / name, package / name)
    frontend = work / "frontend"
    for name in frontend_files:
        copy_file(root / name, work / name)
    for name in ("launcher.py", "启动.cmd", "使用说明.txt"):
        copy_file(root / "scripts/portable" / name, package / name)
    # cmd.exe expects CRLF, and startup uses UTF-8 after chcp.
    cmd = package / "启动.cmd"
    cmd.write_bytes(cmd.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    copy_file(root / "LICENSE", package / "LICENSE")
    run([args.npm, "ci", "--no-audit", "--no-fund"], cwd=frontend)
    run([args.npm, "run", "build"], cwd=frontend)
    shutil.copytree(frontend / "dist", package / "web")
    frontend_dependencies = collect_frontend_licenses(frontend, package)
    ffmpeg = collect_ffmpeg(root, work, package)
    models = collect_models(root, package)
    dependencies = collect_python_licenses(package)
    conda = collect_conda_licenses(work, package)
    (package / "THIRD_PARTY_NOTICES.txt").write_text(third_party_notices(), encoding="utf-8")
    environment_archive = work / "runtime.zip"
    run([args.build_python, "-I", "-m", "conda_pack.cli", "--prefix", work / "env", "--output", environment_archive,
         "--format", "zip", "--compress-level", "4", "--exclude", "*.pyc", "--exclude", "*.pyo",
         "--exclude", "*/__pycache__/*"])
    runtime = package / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(environment_archive) as source:
        source.extractall(runtime)
    # conda-pack may retain empty cache directories, which carry no runtime data.
    for path in sorted(runtime.rglob("__pycache__"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    audit_package(package)
    if input_hashes != {name: sha256(root / name) for name in source_files}:
        raise RuntimeError("Build input changed during assembly; retry from stable sources")
    files = {str(path.relative_to(package)).replace("\\", "/"): {"size": path.stat().st_size, "sha256": sha256(path)}
             for path in sorted(package.rglob("*")) if path.is_file()}
    manifest = {"schema": 1, "product": "StudyBuddy", "version": args.version, "kind": "local-validation-preview",
                "built_at_utc": datetime.now(timezone.utc).isoformat(), "target": "windows-x64-cpu",
                "python": sys.version.split()[0], "conda_pack": "0.9.2", "conda_version": run([args.conda, "--version"], capture_output=True, text=True).stdout.strip(),
                "source": {"commit": git(root, "rev-parse", "HEAD"), "git_version": git(root, "--version"),
                           "status": git(root, "status", "--porcelain", "--untracked-files=no"),
                           "inputs": input_hashes},
                "dependencies": dependencies, "frontend_dependencies": frontend_dependencies, "conda_packages": conda,
                "tools": {"ffmpeg": ffmpeg}, "models": models, "publication_todos": PUBLICATION_TODOS,
                "unpacked_bytes_excluding_manifest": sum(value["size"] for value in files.values()),
                "manifest_excludes_itself": True, "files": files}
    (package / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = root / "dist" / f"StudyBuddy-v{args.version}-windows-x64-preview.zip"
    if zip_path.exists() or zip_path.with_suffix(".zip.sha256").exists():
        raise FileExistsError(zip_path)
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=4, allowZip64=True) as output:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                output.write(path, str(path.relative_to(work)))
    with zip_path.with_suffix(".zip.sha256").open("x", encoding="ascii") as output:
        output.write(sha256(zip_path) + "  " + zip_path.name + "\n")
    print(json.dumps({"zip": str(zip_path), "zip_bytes": zip_path.stat().st_size,
                      "unpacked_bytes": sum(p.stat().st_size for p in package.rglob("*") if p.is_file())}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("root", "work", "build-python", "conda", "npm"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--version", required=True)
    main(parser.parse_args())
