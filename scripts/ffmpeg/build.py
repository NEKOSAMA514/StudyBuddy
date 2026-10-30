"""Build and verify one fixed Windows x64 shared FFmpeg; never install it into the app."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOOLS = ROOT / "dist/.ffmpeg-tools"
SOURCES = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
MSYS_URL = "https://github.com/msys2/msys2-installer/releases/download/2026-09-27/msys2-base-x86_64-20260927.sfx.exe"
MSYS_SHA256 = "ad336cccfda47758b5e15cda993fbba421115cb0b126697daef1ee4dfe37209f"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(args, log, cwd=None, env=None):
    print(">>> " + str(args[0]) + " " + " ".join(str(x) for x in args[1:3]), flush=True)
    with log.open("ab") as output:
        output.write(("\n>>> " + subprocess.list2cmdline([str(x) for x in args]) + "\n").encode())
        output.flush()
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace")[-5000:]
        raise RuntimeError(f"Command failed ({result.returncode}); {log}\n{tail}")


def git(directory, *args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(["git", "-c", "credential.helper=", "-c", "http.sslBackend=openssl",
                             "-C", str(directory), *args], env=env, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8").strip()


def download(url, path, expected):
    if path.exists() and sha256(path) == expected:
        return path
    for attempt in range(3):
        try:
            temporary = path.with_suffix(".partial")
            with urllib.request.urlopen(url, timeout=90) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if sha256(temporary) != expected:
                raise RuntimeError(f"Download SHA-256 mismatch: {path.name}")
            temporary.replace(path)
            return path
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def prepare_tools(log):
    TOOLS.mkdir(parents=True, exist_ok=True)
    msys = TOOLS / "msys64"
    bash = msys / "usr/bin/bash.exe"
    if not bash.is_file():
        archive = download(MSYS_URL, TOOLS / "msys2-20260927.sfx.exe", MSYS_SHA256)
        run([archive, "-y", "-o" + str(TOOLS)], log, cwd=TOOLS)
    bootstrap_env = {**os.environ, "MSYSTEM": "MSYS", "CHERE_INVOKING": "1"}
    marker = TOOLS / "msys-ready.json"
    if not marker.is_file():
        run([bash, "-lc", "true"], log, cwd=TOOLS, env=bootstrap_env)
        for _ in range(2):
            run([bash, "-lc", "pacman --noconfirm -Syuu"], log, cwd=TOOLS, env=bootstrap_env)
        run([bash, "-lc", "pacman --noconfirm -S --needed make nasm pkgconf diffutils"], log, cwd=TOOLS, env=bootstrap_env)
        packages = subprocess.check_output([bash, "-lc", "pacman -Q"], cwd=TOOLS, env=bootstrap_env).decode()
        save(marker, {"bootstrap_url": MSYS_URL, "bootstrap_sha256": MSYS_SHA256, "packages": packages})
    python = TOOLS / "venv/Scripts/python.exe"
    if not python.is_file():
        run([sys.executable, "-m", "venv", TOOLS / "venv"], log)
    wheels = []
    wheel_records = []
    for name, version in (("meson", "1.12.1"), ("ninja", "1.13.2")):
        record_path = TOOLS / f"{name}-{version}.json"
        if record_path.is_file():
            record = json.loads(record_path.read_text(encoding="utf-8"))
        else:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as source:
                data = json.load(source)
            wheel = next(f for f in data["urls"] if f["filename"].endswith("py3-none-any.whl" if name == "meson" else "py3-none-win_amd64.whl"))
            record = {"name": wheel["filename"], "url": wheel["url"], "sha256": wheel["digests"]["sha256"]}
            save(record_path, record)
        wheels.append(download(record["url"], TOOLS / record["name"], record["sha256"]))
        wheel_records.append(record)
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-index", *wheels], log)
    save(TOOLS / "python-tools.json", wheel_records)
    return bash, python


def compiler_environment():
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    result = subprocess.check_output([vswhere, "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                                      "-property", "installationPath"], encoding="utf-8").strip().splitlines()
    if not result:
        raise RuntimeError("MSVC x64 build tools are required")
    vcvars = Path(result[0]) / "VC/Auxiliary/Build/vcvars64.bat"
    command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{vcvars}" -vcvars_ver=14.44 >nul && set"'
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError("Unable to initialize MSVC 14.44: " + result.stderr.decode(errors="replace"))
    env = {}
    for line in result.stdout.decode("mbcs").splitlines():
        if "=" in line and not line.startswith("="):
            key, value = line.split("=", 1)
            env[key.upper()] = value
    if not env.get("VCTOOLSVERSION", "").startswith("14.44."):
        raise RuntimeError("The required MSVC 14.44 compiler was not selected")
    return env


def prepare_source(name, spec, destination):
    destination.mkdir(parents=True)
    git(destination, "init", "--template=", ".")
    git(destination, "config", "core.autocrlf", "false")
    key = hashlib.sha256((spec["repo"] + "\n" + spec["commit"]).encode()).hexdigest()[:24]
    cache = ROOT / "dist/ffmpeg-source-cache/git" / key
    origin = str(cache.resolve()) if (cache / "HEAD").is_file() else spec["repo"]
    git(destination, "fetch", "--depth=1", "--no-tags", origin, spec["commit"])
    git(destination, "checkout", "--detach", "FETCH_HEAD")
    if git(destination, "rev-parse", "HEAD") != spec["commit"] or git(destination, "rev-parse", "HEAD^{tree}") != spec["tree"]:
        raise RuntimeError(f"Source identity mismatch: {name}")
    archive = destination.parent.parent / "source-archives" / (name + ".tar.gz")
    archive.parent.mkdir(exist_ok=True)
    git(destination, "archive", "--format=tar.gz", "--prefix=" + name + "/", "--output=" + str(archive), "HEAD")
    print(f"SOURCE {name} {spec['commit'][:12]}", flush=True)
    return {**spec, "archive": archive.name, "sha256": sha256(archive)}


def archive_directory(directory, destination):
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory.parent).as_posix())


def verify_and_package(work, output, manifest, env):
    log = output / "build.log"
    package = output / "ffmpeg-win64"
    for library in (package / "bin").glob("*.lib"):
        library.replace(package / "lib" / library.name)
    licenses = package / "licenses"
    licenses.mkdir(exist_ok=True)
    for name in SOURCES:
        target = licenses / name
        target.mkdir(exist_ok=True)
        for path in (work / "src" / name).iterdir():
            if path.is_file() and path.name.lower().startswith(("copying", "license")):
                shutil.copyfile(path, target / path.name)
    (package / "README.txt").write_text(
        "StudyBuddy Windows x64 FFmpeg: GPLv3 build with x264 and dav1d.\n"
        "bin/: executables and FFmpeg DLLs. include/ and lib/: development files for a later PyAV build.\n"
        "This package does not include PyAV. Existing application tools were not replaced.\n"
        "See build-manifest.json, licenses/, and the matching source archive.\n", encoding="utf-8")
    reference = ROOT / "tools/ffmpeg/bin/ffmpeg.exe"
    run([sys.executable, "-X", "utf8", "-B", HERE / "verify.py", "--package", package,
         "--reference", reference, "--output", output / "validation", "--dumpbin", Path(env["VCTOOLSINSTALLDIR"]) / "bin/Hostx64/x64/dumpbin.exe"],
        log, cwd=ROOT)
    shutil.copyfile(log, package / "build.log")
    shutil.copyfile(output / "validation/report.json", package / "validation-report.json")
    shutil.copyfile(work / "ffmpeg-configure.txt", package / "ffmpeg-configure.txt")
    for name in SOURCES:
        expected = (HERE / manifest["source_patches"][name]["file"]).read_text(encoding="utf-8").strip() if name in manifest["source_patches"] else ""
        if git(work / "src" / name, "diff", "--binary") != expected:
            raise RuntimeError(f"Unrecorded source modifications: {name}")
    manifest["configuration"] = (work / "ffmpeg-configure.txt").read_text(encoding="utf-8")
    manifest["files"] = {p.relative_to(package).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                         for p in sorted(package.rglob("*")) if p.is_file()}
    source_package = work / "ffmpeg-sources"
    source_package.mkdir()
    for path in (work / "source-archives").iterdir():
        shutil.copyfile(path, source_package / path.name)
    shutil.copytree(HERE, source_package / "scripts/ffmpeg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "scripts/build-ffmpeg.ps1", source_package / "scripts/build-ffmpeg.ps1")
    shutil.copyfile(ROOT / "LICENSE", source_package / "LICENSE")
    (source_package / "docs").mkdir()
    shutil.copyfile(ROOT / "docs/ffmpeg-build.md", source_package / "docs/ffmpeg-build.md")
    (source_package / "BUILD.txt").write_text(
        "Sources are pinned in scripts/ffmpeg/sources.json. Source archives contain the corresponding unmodified trees.\n"
        "Run scripts/build-ffmpeg.ps1 from a source checkout with Python 3.12+, Git and MSVC 14.44.\n"
        "Build commands are in scripts/ffmpeg/build.sh. Actual compiler, SDK and tool versions are recorded in build-manifest.json.\n"
        "x264 and dav1d are static; FFmpeg libraries are shared. All use the static MSVC runtime.\n"
        "The FFmpeg mswindres UTF-8 patch is in scripts/ffmpeg/patches; build.sh applies it before configuration.\n"
        "This is not a byte-for-byte reproducibility claim.\n", encoding="utf-8")
    shutil.copyfile(work / "ffmpeg-configure.txt", source_package / "ffmpeg-configure.txt")
    manifest["status"] = "verified"
    save(output / "build-manifest.json", manifest)
    shutil.copyfile(output / "build-manifest.json", package / "build-manifest.json")
    shutil.copyfile(output / "build-manifest.json", source_package / "build-manifest.json")
    archive_directory(package, output / "ffmpeg-win64.zip")
    archive_directory(source_package, output / "ffmpeg-sources.zip")
    sums = "".join(sha256(output / name) + "  " + name + "\n" for name in ("ffmpeg-win64.zip", "ffmpeg-sources.zip", "build-manifest.json"))
    (output / "SHA256SUMS.txt").write_text(sums, encoding="ascii")
    print(f"VERIFIED {output}", flush=True)


def build():
    if os.name != "nt" or sys.version_info < (3, 12):
        raise RuntimeError("Use native Windows x64 Python 3.12 or later")
    identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    work = ROOT / "dist/.ffmpeg-build" / identifier
    output = ROOT / "dist/ffmpeg-minimal" / identifier
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    log = output / "build.log"
    manifest = {"status": "building", "created_at": datetime.now(timezone.utc).isoformat(), "target": "Windows x64",
                "external_media_libraries": ["x264", "dav1d"], "pyav_built": False,
                "default_tools_changed": False, "work_directory": str(work), "sources": {},
                "source_patches": {"ffmpeg": {"file": "patches/ffmpeg-mswindres-utf8.patch",
                                             "sha256": sha256(HERE / "patches/ffmpeg-mswindres-utf8.patch")}}}
    save(output / "build-manifest.json", manifest)
    print(f"OUTPUT {output}", flush=True)
    try:
        for name, spec in SOURCES.items():
            manifest["sources"][name] = prepare_source(name, spec, work / "src" / name)
        save(output / "build-manifest.json", manifest)
        bash, python = prepare_tools(log)
        env = compiler_environment()
        env.update(STUDY_BUILD_WORK=str(work), STUDY_BUILD_PREFIX=str(output / "ffmpeg-win64"),
                   STUDY_BUILD_PYTHON=str(python), STUDY_BUILD_TOOLS=str(TOOLS),
                   MSYSTEM="MSYS", CHERE_INVOKING="1", MSYS2_ARG_CONV_EXCL="/std:",
                   MSYS2_ENV_CONV_EXCL="INCLUDE;LIB;LIBPATH", VSLANG="1033")
        env["PATH"] = str(TOOLS / "msys64/usr/bin") + ";" + env["PATH"]
        manifest["compiler"] = {key: env.get(key.upper()) for key in ("VCToolsVersion", "WindowsSDKVersion", "VCToolsInstallDir")}
        manifest["tools"] = json.loads((TOOLS / "msys-ready.json").read_text(encoding="utf-8"))
        manifest["tools"].update(
            meson=subprocess.check_output([python, "-m", "mesonbuild.mesonmain", "--version"]).decode().strip(),
            ninja=subprocess.check_output([python, "-m", "ninja", "--version"]).decode().strip(), python=sys.version)
        manifest["tools"]["python_wheels"] = json.loads((TOOLS / "python-tools.json").read_text(encoding="utf-8"))
        save(output / "build-manifest.json", manifest)
        run([bash, "--noprofile", "--norc", str(HERE / "build.sh")], log, cwd=work, env=env)
        verify_and_package(work, output, manifest, env)
    except Exception as error:
        manifest.update(status="failed", error=str(error))
        save(output / "build-manifest.json", manifest)
        raise


if __name__ == "__main__":
    try:
        build()
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
