"""通过真实 API 上传指定本地视频。不会伪造识别或总结结果。"""
import argparse
import json
from pathlib import Path
import time
import httpx


parser = argparse.ArgumentParser()
parser.add_argument("path", type=Path)
parser.add_argument("--base", default="http://host.docker.internal:18000")
parser.add_argument("--wait", action="store_true")
args = parser.parse_args()
with httpx.Client(timeout=120) as client:
    with args.path.open("rb") as file:
        response = client.post(args.base + "/api/v1/videos", files={"file": (args.path.name, file, "video/mp4")})
    response.raise_for_status()
    video = response.json()
    response = client.post(args.base + f'/api/v1/videos/{video["id"]}/analyses')
    response.raise_for_status()
    created = response.json()
    print(json.dumps({"video_id": video["id"], "task_id": created["task_id"], "duration": video["duration"]}), flush=True)
    if args.wait:
        deadline, previous = time.monotonic() + 1800, None
        while time.monotonic() < deadline:
            response = client.get(args.base + f'/api/v1/tasks/{created["task_id"]}')
            response.raise_for_status()
            task = response.json()
            state = (task["status"], task["stage"])
            if state != previous:
                print(json.dumps({"status": state[0], "stage": state[1]}, ensure_ascii=False), flush=True)
                previous = state
            if state[0] in {"succeeded", "failed", "interrupted"}:
                print(json.dumps({"error": task["error"], "timings": task["timings"]}, ensure_ascii=False), flush=True)
                break
            time.sleep(3)
