import ast
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from app.recognition import chinese_asr, model_catalog, model_files
from app.config import Settings


@pytest.mark.parametrize("identifier", ["paraformer", "whisper-small", "whisper-base", "rapidocr"])
def test_readiness_requires_all_nonempty_files_in_shared_directory(tmp_path, identifier):
    settings = Settings(model_dir=tmp_path)
    directory = model_catalog.model_path(settings, identifier)
    assert directory == tmp_path / model_catalog.MODELS[identifier]["directory"]
    assert not model_catalog.model_ready(settings, identifier)
    for filename in model_catalog.MODELS[identifier]["files"]:
        file = directory / filename
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"test weight")
    assert model_catalog.model_ready(settings, identifier)
    file.write_bytes(b"")
    assert not model_catalog.model_ready(settings, identifier)
    file.unlink()
    file.mkdir()
    assert not model_catalog.model_ready(settings, identifier)


@pytest.mark.parametrize("identifier,dependency", [
    ("whisper-small", "faster_whisper"), ("rapidocr", "rapidocr"), ("paraformer", "sherpa_onnx")])
def test_runtime_probe_preserves_package_lookup(monkeypatch, identifier, dependency):
    calls, ready = [], True
    def probe(name):
        calls.append(name)
        return object() if ready else None
    monkeypatch.setattr(model_catalog.importlib.util, "find_spec", probe)
    assert model_catalog.runtime_ready(identifier)
    ready = False
    assert not model_catalog.runtime_ready(identifier)
    assert calls == [dependency, dependency]


def test_paraformer_load_keeps_pinned_download_and_factory_arguments(tmp_path, monkeypatch):
    settings = Settings(model_dir=tmp_path, asr_threads=3)
    directory = model_catalog.model_path(settings, "paraformer")
    directory.mkdir()
    (directory / "model.int8.onnx").write_bytes(b"local test weight")
    downloads, verified, factory = [], [], []
    def download(repo, filename, **kwargs):
        downloads.append((repo, filename, kwargs))
        file = kwargs["local_dir"] / filename
        file.write_bytes(b"fake downloaded tokens")
        return str(file)
    def recognize(**kwargs):
        factory.append(kwargs)
        return "fake paraformer"
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=download))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(OfflineRecognizer=SimpleNamespace(from_paraformer=recognize)))
    monkeypatch.setattr(chinese_asr, "verify_paraformer", lambda path: verified.append(path))
    recognizer = chinese_asr.ChineseASR(settings)
    recognizer.load()
    assert recognizer.model == "fake paraformer"
    assert downloads == [("csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14", "tokens.txt",
                          {"revision": "def027084691107096b5ebba69785756d63de6c5", "local_dir": directory})]
    assert verified == [directory]
    assert factory == [{"paraformer": str(directory / "model.int8.onnx"), "tokens": str(directory / "tokens.txt"),
                        "num_threads": 3, "sample_rate": 16000, "feature_dim": 80,
                        "decoding_method": "greedy_search", "provider": "cpu"}]


def test_model_dependencies_are_acyclic_including_function_imports():
    names = {"local_models", "model_catalog", "model_files", "chinese_asr", "media"}
    directory = Path(model_catalog.__file__).parent
    graph = {name: set() for name in names}
    for name in names:
        for node in ast.walk(ast.parse((directory / f"{name}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                target = node.module if node.level else (node.module or "").removeprefix("app.recognition.")
                if target in names:
                    graph[name].add(target)
            elif isinstance(node, ast.Import):
                graph[name].update(item.name.removeprefix("app.recognition.") for item in node.names if item.name.removeprefix("app.recognition.") in names)
    assert not graph["local_models"] & {"chinese_asr", "media"}
    assert "local_models" not in graph["chinese_asr"] | graph["media"]
    def check(name, ancestors):
        assert name not in ancestors, f"circular dependency: {ancestors} -> {name}"
        for child in graph[name]:
            check(child, ancestors + [name])
    for name in names:
        check(name, [])


def test_importing_model_modules_does_not_load_optional_recognition_libraries():
    code = '''
import builtins, importlib, sys
blocked = {"faster_whisper", "sherpa_onnx", "huggingface_hub", "rapidocr", "PIL", "numpy", "onnxruntime", "ctranslate2"}
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert name.split(".")[0] not in blocked, "eager recognition import: " + name
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
for name in ("model_catalog", "model_files", "local_models", "chinese_asr", "media"):
    importlib.import_module("app.recognition." + name)
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
