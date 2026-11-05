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


FFMPEG_SOURCES = "scripts/ffmpeg/sources.json"
FFMPEG_PATCH = "scripts/ffmpeg/patches/ffmpeg-mswindres-utf8.patch"
FFMPEG_RUNTIME = (
    "ffmpeg.exe", "ffprobe.exe", "avcodec-61.dll", "avdevice-61.dll", "avfilter-10.dll",
    "avformat-61.dll", "avutil-59.dll", "postproc-58.dll", "swresample-5.dll", "swscale-8.dll",
)
FFMPEG_LICENSES = (
    "ffmpeg/COPYING.GPLv2", "ffmpeg/COPYING.GPLv3", "ffmpeg/COPYING.LGPLv2.1",
    "ffmpeg/COPYING.LGPLv3", "ffmpeg/LICENSE.md", "x264/COPYING", "dav1d/COPYING",
)
LICENSE_ASSETS = Path(__file__).with_name("licenses")
PUBLICATION_TODOS = [
    "Windows 10 和 Windows 11 x64 干净系统验收尚未完成。",
    "真实云端模型和 MinerU API 验收尚未完成。",
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


def collect_ffmpeg(root, build, package):
    sums = {}
    for line in (build / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        digest, name = line.split(maxsplit=1)
        sums[name] = digest
    for name in ("ffmpeg-win64.zip", "ffmpeg-sources.zip", "build-manifest.json"):
        if sha256(build / name) != sums.get(name):
            raise RuntimeError(f"FFmpeg SHA-256 mismatch: {name}")

    manifest_bytes = (build / "build-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["status"] != "verified" or manifest["target"] != "Windows x64":
        raise RuntimeError("A verified Windows x64 FFmpeg build is required")
    sources = json.loads((root / FFMPEG_SOURCES).read_text(encoding="utf-8"))
    built_sources = {name: {key: record[key] for key in ("repo", "commit", "tree")}
                     for name, record in manifest["sources"].items()}
    if built_sources != sources or set(manifest["external_media_libraries"]) != set(sources) - {"ffmpeg"}:
        raise RuntimeError("FFmpeg build sources do not match scripts/ffmpeg/sources.json")
    patch = {"file": "patches/ffmpeg-mswindres-utf8.patch", "sha256": sha256(root / FFMPEG_PATCH)}
    if manifest["source_patches"] != {"ffmpeg": patch}:
        raise RuntimeError("FFmpeg build patch does not match the repository")
    with zipfile.ZipFile(build / "ffmpeg-sources.zip") as archive:
        if archive.read("ffmpeg-sources/build-manifest.json") != manifest_bytes:
            raise RuntimeError("FFmpeg source archive belongs to a different build")

    files = {f"bin/{name}": f"tools/{name}" for name in FFMPEG_RUNTIME}
    files.update({f"licenses/{name}": f"licenses/ffmpeg/{name}" for name in FFMPEG_LICENSES})
    with zipfile.ZipFile(build / "ffmpeg-win64.zip") as archive:
        if archive.read("ffmpeg-win64/build-manifest.json") != manifest_bytes:
            raise RuntimeError("FFmpeg binary archive belongs to a different build")
        report = json.loads(archive.read("ffmpeg-win64/validation-report.json"))
        if report["status"] != "passed":
            raise RuntimeError("FFmpeg media validation did not pass")
        for name, output in files.items():
            payload = archive.read(f"ffmpeg-win64/{name}")
            record = manifest["files"][name]
            if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise RuntimeError(f"FFmpeg build file does not match its manifest: {name}")
            destination = package / output
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    return {"version": report["version"].split()[2], "sha256": sums["ffmpeg-win64.zip"],
            "source_archive": "ffmpeg-sources.zip", "source_sha256": sums["ffmpeg-sources.zip"],
            "build_manifest_sha256": sums["build-manifest.json"], "sources": manifest["sources"],
            "source_patches": manifest["source_patches"], "external_media_libraries": manifest["external_media_libraries"]}


def collect_models(root, package):
    sys.path.insert(0, str(package / "backend"))
    from app.recognition.model_catalog import MODELS, bundled_ocr_path
    from app.recognition.model_files import verify_paraformer
    results = {}
    for identifier in ("paraformer", "rapidocr"):
        spec = MODELS[identifier]
        if identifier == "paraformer":
            directory = f"models/{spec['directory']}"
            target = package / directory
            target.mkdir(parents=True)
            base = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}"
            for name in spec["files"]:
                local = root / directory / name
                if local.is_file():
                    copy_file(local, target / name)
                else:
                    download(f"{base}/{name}", target / name)
            verify_paraformer(target)
            for name in ("README.md",):
                download(base + "/" + name, package / "licenses/models/paraformer" / name)
            results[identifier] = {"repo": spec["repo"], "revision": spec["revision"], "source": base, "directory": directory}
        else:
            # conda-pack 已包含 wheel 内置权重，记录其随包位置。
            target = bundled_ocr_path()
            if target is None:
                raise RuntimeError("RapidOCR package is missing")
            results[identifier] = {"distribution": "rapidocr", "version": metadata.version("rapidocr"),
                                   "source": "https://github.com/RapidAI/RapidOCR",
                                   "directory": "runtime/Lib/site-packages/rapidocr/models"}
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
                "web/index.html", *(f"tools/{name}" for name in FFMPEG_RUNTIME),
                "licenses/conda-explicit.txt", "docs/SOURCE.md")
    for name in required:
        if not (package / name).is_file():
            raise RuntimeError(f"Missing portable file: {name}")
    top = {"启动.cmd", "launcher.py", "使用说明.txt", "LICENSE", "THIRD_PARTY_NOTICES.txt", "release-manifest.json",
           "runtime", "backend", "web", "tools", "models", "licenses", "docs"}
    prohibited = {".git", ".env", "__pycache__", ".pytest_cache", ".idea", "test-results", "playwright-report"}
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if path.is_symlink() or relative.parts[0] not in top:
            raise RuntimeError(f"Unexpected package path: {relative}")
        if any(part in prohibited or part.startswith(".env.") or part.lower().startswith("pymupdf") for part in relative.parts):
            raise RuntimeError(f"Prohibited package path: {relative}")
        if relative.parts[0] == "backend" and len(relative.parts) > 1 and relative.parts[1] != "app":
            raise RuntimeError(f"Non-business backend path: {relative}")
        if path.is_file() and path.suffix.lower() in {".pyc", ".pyo", ".log", ".pdb"}:
            raise RuntimeError(f"Cache/log/debug file in package: {relative}")


def main(args):
    root, work = args.root.resolve(), args.work.resolve()
    if not work.is_relative_to(root / "dist/.build") or work == root / "dist/.build":
        raise RuntimeError("Build work directory must be a unique child of dist/.build")
    package = work / "StudyBuddy-Windows-x64"
    package.mkdir()
    backend, frontend_files = tracked_inputs(root)
    source_files = backend + frontend_files + ["backend/requirements.lock", "LICENSE", "scripts/build-portable.ps1",
        "scripts/portable/build.py", "scripts/portable/launcher.py", "scripts/portable/启动.cmd", "scripts/portable/使用说明.txt",
        "docs/SOURCE.md", FFMPEG_SOURCES, FFMPEG_PATCH]
    source_files += [str(path.relative_to(root)).replace("\\", "/") for path in sorted(LICENSE_ASSETS.iterdir()) if path.is_file()]
    input_hashes = {name: sha256(root / name) for name in source_files}
    ffmpeg = collect_ffmpeg(root, args.ffmpeg_build, package)
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
    copy_file(root / "docs/SOURCE.md", package / "docs/SOURCE.md")
    run([args.npm, "ci", "--no-audit", "--no-fund"], cwd=frontend)
    run([args.npm, "run", "build"], cwd=frontend)
    shutil.copytree(frontend / "dist", package / "web")
    frontend_dependencies = collect_frontend_licenses(frontend, package)
    models = collect_models(root, package)
    dependencies = collect_python_licenses(package)
    conda = collect_conda_licenses(work, package)
    notices = ("StudyBuddy — 第三方说明\n\n项目自有代码使用 GPLv3（GPL-3.0-only）；第三方软件和模型保留各自许可。\n"
               "Python、Conda、前端和模型的原始许可材料分别见 licenses/python、licenses/conda、licenses/frontend、licenses/models。\n"
               "依赖版本、来源和文件信息见 release-manifest.json。\n"
               "对应源码获取方式见 docs/SOURCE.md。\n"
               "自建 FFmpeg、x264 和 dav1d 的许可证见 licenses/ffmpeg。独立 FFmpeg 的构建和源码归档校验值见 release-manifest.json。\n\n"
               "尚未验证事项：\n" + "\n".join("- " + item for item in PUBLICATION_TODOS) + "\n")
    (package / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf-8")
    environment_archive = work / "runtime.zip"
    run([args.build_python, "-I", "-m", "conda_pack.cli", "--prefix", work / "env", "--output", environment_archive,
         "--format", "zip", "--compress-level", "4", "--exclude", "*.pyc", "--exclude", "*.pyo",
         "--exclude", "*.pdb", "--exclude", "*/__pycache__/*"])
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
    for name in ("root", "work", "build-python", "conda", "npm", "ffmpeg-build"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--version", required=True)
    main(parser.parse_args())
