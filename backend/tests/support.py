"""跨测试模块共用的文件样本；不依赖测试收集。"""
import json
import shutil
from app.recognition.local_models import MODELS, model_path


def fake_files(settings, identifier):
    for file in MODELS[identifier]["files"]:
        path = model_path(settings, identifier) / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test model")


def completed_download(app, source, status="succeeded"):
    downloads = app.state.video_downloads
    identifier = "d" * 32
    folder = downloads.directory / "下载课堂-360P"
    folder.mkdir()
    path = folder / (folder.name + source.suffix)
    shutil.copyfile(source, path)
    state = {"id": identifier, "status": status, "path": str(path)}
    (folder / "task.json").write_text(json.dumps(state), encoding="utf-8")
    downloads.locations[identifier] = folder
    return identifier, path
