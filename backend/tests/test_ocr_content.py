from .fakes import FakeModel

from app.db import Video
from app.videos.pipeline import Pipeline
from app.videos.schemas import ground_timestamps
from .conftest import upload


SCOPE = "可测定 Cl-、Br-、I-、SCN-、PO4^3-、AsO4^3- 等。"


def test_ocr_only_citation_uses_its_frame_timestamp():
    evidence = {
        "speech-real": {"id": "speech-real", "kind": "speech", "start_seconds": 155, "end_seconds": 170, "text": "流程"},
        "frame-real": {"id": "frame-real", "kind": "frame", "start_seconds": 0, "end_seconds": 0, "text": SCOPE},
    }
    assert ground_timestamps(["frame-real"], evidence, 180) == {
        "start_seconds": 0, "end_seconds": 0, "timestamp_basis": "frame",
    }


def test_pipeline_keeps_ocr_only_content_and_uses_speech_for_time(client, app, video_file, monkeypatch):
    video = upload(client, video_file)
    task_id = client.post("/api/v1/videos/" + video["id"] + "/analyses").json()["task_id"]
    with app.state.sessions.begin() as session:
        session.get(Video, video["id"]).audio_codec = "aac"
    monkeypatch.setattr("app.videos.pipeline.extract_audio", lambda source, target, media_info=None: target.write_bytes(b"fake-audio"))
    inputs = []
    class Recognizer:
        def transcribe(self, path):
            return [{"start_seconds": 0.8, "end_seconds": 1.5, "text": "先加入过量银离子，再进行返滴定。"}]
        def ocr(self, path):
            return SCOPE, 99
    class Model(FakeModel):
        async def generate(self, operation, payload, validate):
            inputs.append((operation, payload))
            if operation == "chapters":
                chapter = payload["chapters"][0]
                frame = next(item for item in chapter["primary"] if item["kind"] == "frame")
                value = {"chapters": [{"chapter_id": chapter["chapter_id"], "summary": "流程与适用范围",
                         "markdown": SCOPE, "issues": []}]}
                validate(value)
                return value
            return await super().generate(operation, payload, validate)
    Pipeline(app.state.pipeline.get_settings, app.state.sessions, app.state.diagnostics, lambda settings: Recognizer(), Model).run(task_id)
    response = client.get("/api/v1/analyses/" + task_id)
    assert response.status_code == 200, response.text
    assert [operation for operation, _ in inputs] == ["outline", "chapters", "overview"]
    assert any(item["text"] == SCOPE for item in inputs[0][1]["evidence"])
    assert any(item["text"] == SCOPE for item in inputs[1][1]["chapters"][0]["primary"])
    assert "evidence" not in inputs[2][1]
    block = response.json()["chapters"][0]
    assert block["start_seconds"] == 0 and block["end_seconds"] == video["duration"]
    assert block["markdown"] == SCOPE and block["timestamp_basis"] == "speech"
    assert any("-frame-" in identifier for identifier in block["evidence_ids"])
    assert SCOPE in client.get("/api/v1/analyses/" + task_id + "/notes").text
