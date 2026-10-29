import subprocess
import pytest
import httpx
from fastapi.testclient import TestClient
from app.config import Settings
from app.db import Task
from app.main import create_app
from app.analysis.llm import RequestPacer


@pytest.fixture(autouse=True)
def block_unmocked_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Backend tests must mock HTTP transport; live requests are disabled")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


@pytest.fixture(autouse=True)
def isolate_request_timing(monkeypatch):
    # 通用回归不等待真实间隔；实际节奏由test_request_pacing的虚拟时钟单独验收。
    async def no_wait(self):
        pass
    monkeypatch.setattr(RequestPacer, "wait", no_wait)


@pytest.fixture
def small_outline_budget(monkeypatch):
    # 固定算法样本预算，与用户调整的生产默认值分开验证。
    original = Settings.__init__
    def initialize(self, *args, **kwargs):
        kwargs.setdefault("llm_input_text_limit", 15000)
        original(self, *args, **kwargs)
    monkeypatch.setattr(Settings, "__init__", initialize)
    monkeypatch.setattr("app.videos.segments.DEFAULT_INPUT_TEXT_LIMIT", 15000)


@pytest.fixture(scope="session")
def video_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "lecture.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=640x360:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", model_dir=tmp_path / "models", log_dir=tmp_path / "log", static_dir=tmp_path / "absent", llm_key="", free_tier_confirmed=False)
    return create_app(settings, start_worker=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def upload(client, path):
    with path.open("rb") as file:
        response = client.post("/api/v1/videos", files={"file": (path.name, file, "video/mp4")})
    assert response.status_code == 201, response.text
    return response.json()


def stored_task_config(app, task_id):
    from app.db import Task
    with app.state.sessions() as session:
        return dict(session.get(Task, task_id).config)


@pytest.fixture
def generation(client, app, video_file, monkeypatch):
    from app.db import Video
    from app.videos.pipeline import Pipeline
    from .fakes import FakeModel, FakeRecognizer
    video = upload(client, video_file)
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, output, media_info=None, cancel=None: output.write_bytes(b"test audio"))
    calls = {"outline": 0, "chapters": 0, "overview": 0}

    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            calls[operation] += 1
            value = await super().generate(operation, payload, validate)
            return {**value, "title": f"测试笔记 {calls['overview']}"} if operation == "overview" else value

    recognizer = FakeRecognizer(None)
    pipeline = Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: recognizer, Model)
    app.state.worker.register("video", Task, pipeline)
    task_id = client.post(f'/api/v1/videos/{video["id"]}/analyses').json()["task_id"]
    return video["id"], task_id, pipeline, recognizer, calls, Model
