"""真实 API 上传/识别/错误保留验证。未配置免费密钥时，不期望出现总结。

测试视频为 scripts/validate_real.py --chinese 生成的自编材料。
"""
import json
import os
from pathlib import Path
import time
import httpx


base = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000") + "/api/v1"
folder = Path(os.getenv("DATA_DIR", "/data")) / "validation"
with httpx.Client(timeout=60) as client:
    with (folder / "sample-chinese.mp4").open("rb") as file:
        response = client.post(base + "/videos", files={"file": ("中文教学验证样本.mp4", file, "video/mp4")})
    response.raise_for_status()
    video = response.json()
    response = client.post(base + f'/videos/{video["id"]}/analyses')
    response.raise_for_status()
    task_id = response.json()["task_id"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        response = client.get(base + f"/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "interrupted"}:
            break
        time.sleep(2)
    assert task["status"] in {"succeeded", "failed"}, task["status"]
    evidence = client.get(base + f'/videos/{video["id"]}/evidence', params={"task_id": task_id}).json()
    assert {e["kind"] for e in evidence} == {"frame", "speech"}
    assert all(0 <= e["start_seconds"] <= e["end_seconds"] <= video["duration"] for e in evidence)
    health = client.get(base + "/health").json()
    if not health["model_configured"]:
        assert task["status"] == "failed" and task["error"]["code"] == "model_not_configured"
        assert task["result"] is None
    report = {"video_id": video["id"], "task_id": task_id, "status": task["status"],
              "error": task["error"], "evidence_count": len(evidence), "timings": task["timings"],
              "model_configured": health["model_configured"], "fake_services_used": False}
    (folder / "real-api.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
