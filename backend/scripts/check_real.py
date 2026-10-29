"""Opt-in real model checks. Read-only model/config mounts; reports use isolated data."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.diagnostics import Diagnostics
from app.errors import AppError
from app.recognition.local_models import RecognitionInput, recognition_candidate
from app.recognition.media import Recognizer
from app.videos.video_reader import extract_audio, extract_frame, probe
from app.settings.model_store import load_settings


def check_summary(result, records, duration):
    evidence = {item["id"]: item for item in records}
    assert 1 <= len(result["title"].strip()) <= 30
    assert 1 <= len(result["tags"]) <= 8 and all(1 <= len(tag.strip()) <= 20 for tag in result["tags"])
    assert result["overview_paragraphs"] and all(len(text) <= 240 for text in result["overview_paragraphs"])
    assert 1 <= len(result["overview_paragraphs"]) <= 6
    assert 1 <= len(result["sections"]) <= 5
    citations = 0
    previous = -1
    for section in result["sections"]:
        assert previous <= section["start_seconds"] <= section["end_seconds"] <= duration
        previous = section["start_seconds"]
        assert 1 <= len(section["points"]) <= 2
        references = []
        for point in section["points"]:
            assert point["evidence_ids"]
            for identifier in point["evidence_ids"]:
                references.append(evidence[identifier])
                citations += 1
        assert section["start_seconds"] == min(item["start_seconds"] for item in references)
        assert section["end_seconds"] == max(item["end_seconds"] for item in references)
    return {"title": result["title"], "tags": result["tags"], "paragraphs": len(result["overview_paragraphs"]), "sections": len(result["sections"]), "citations": citations}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", default="paraformer")
    parser.add_argument("--ocr", default="rapidocr")
    parser.add_argument("--language", default="zh", choices=["zh", "en", "auto"])
    parser.add_argument("--cloud", action="store_true")
    args = parser.parse_args()
    output = Path("/test-output")
    settings = Settings(llm_key="", free_tier_confirmed=False)
    report = {"fake_services_used": False, "cloud_requested": args.cloud, "cloud_model_called": False,
              "asr": args.asr, "ocr": args.ocr, "language": args.language, "status": "failed"}
    diagnostics = Diagnostics(settings.log_dir, secret=lambda: settings.llm_key)
    started = time.monotonic()
    try:
        settings.prepare()
        settings = recognition_candidate(settings, RecognitionInput(asr_option=args.asr, ocr_option=args.ocr, language=args.language))
        info = probe(Path("/input.mp4"), settings.max_duration)
        report["duration"] = info["duration"]
        if args.cloud:
            settings = replace(load_settings(replace(settings, data_dir=Path("/saved-settings"))), data_dir=settings.data_dir)
            if not settings.llm_key:
                raise AppError("model_not_configured", "请先在设置中保存 API 密钥。")
            from fastapi.testclient import TestClient
            from app.main import create_app
            from app.analysis.llm import ModelClient
            from app.videos.pipeline import Pipeline

            class ObservedModel(ModelClient):
                def response(self, client, payload):
                    report["cloud_model_called"] = True
                    return super().response(client, payload)

            def pipeline(actual, sessions):
                return Pipeline(actual, sessions, model_factory=ObservedModel)

            # A real production factory in a private test directory, never the running user's service.
            with TestClient(create_app(settings, pipeline_factory=pipeline)) as client:
                with Path("/input.mp4").open("rb") as source:
                    response = client.post("/api/v1/videos", files={"file": ("real-test.mp4", source, "video/mp4")})
                response.raise_for_status()
                video = response.json()
                response = client.post(f'/api/v1/videos/{video["id"]}/analyses')
                response.raise_for_status()
                task_id = response.json()["task_id"]
                report.update(video_id=video["id"], task_id=task_id)
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    response = client.get(f"/api/v1/tasks/{task_id}")
                    response.raise_for_status()
                    task = response.json()
                    if task["status"] in {"succeeded", "failed", "interrupted", "paused"}:
                        break
                    time.sleep(1)
                report.update(task_status=task["status"], timings=task["timings"], error=task["error"])
                assert task["status"] == "succeeded", f'Task ended in {task["status"]}; inspect the isolated log.'
                response = client.get(f'/api/v1/videos/{video["id"]}/evidence', params={"task_id": task_id})
                response.raise_for_status()
                report["summary_checks"] = check_summary(task["result"], response.json(), info["duration"])
                (output / "real-result.json").write_text(json.dumps(task["result"], ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            recognizer = Recognizer(settings)
            audio = output / "audio.wav"
            extract_audio(Path("/input.mp4"), audio)
            clock = time.monotonic()
            speech = recognizer.transcribe(audio)
            assert speech, "Real ASR returned no speech. Use a narrated teaching video."
            assert all(0 <= item["start_seconds"] <= item["end_seconds"] <= info["duration"] for item in speech)
            report.update(asr_seconds=round(time.monotonic() - clock, 3), speech=speech)
            frames = []
            clock = time.monotonic()
            for fraction in [0, 0.2, 0.4, 0.6, 0.8]:
                seconds = round(info["duration"] * fraction, 3)
                image = output / f"frame-{seconds}.jpg"
                extract_frame(Path("/input.mp4"), image, seconds)
                text, confidence = recognizer.ocr(image)
                frames.append({"seconds": seconds, "text": text, "confidence": confidence})
            report.update(ocr_seconds=round(time.monotonic() - clock, 3), frames=frames)
            assert any(frame["text"].strip() for frame in frames), "Real OCR returned no text in five samples."
        report["status"] = "passed"
    except Exception as exc:
        reference = diagnostics.record("real_test_failed", "真实模型测试失败。", exc)
        report["failure"] = {"type": type(exc).__name__, **reference}
    finally:
        report["seconds"] = round(time.monotonic() - started, 3)
        (output / "real-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnostics.close()
    print(json.dumps({key: value for key, value in report.items() if key not in {"speech", "frames"}}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
