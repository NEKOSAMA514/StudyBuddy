from .fakes import model_validator
from app.analysis.content_types import CONTENT_TYPES
from .fakes import outline_payload, outline_result
import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app.analysis import llm
from app.videos.detailed import DetailedVideoGeneration
from app.errors import TaskPaused
from app.videos.model import ModelClient
from app.config import Settings
from app.diagnostics import Diagnostics
from .fakes import model_content

paced_wait = llm.RequestPacer.wait


class Clock:
    def __init__(self):
        self.now, self.waiting = 0.0, []
        self.yield_loop = asyncio.sleep

    async def sleep(self, delay):
        if delay <= 0:
            await self.yield_loop(0)
            return
        future = asyncio.get_running_loop().create_future()
        self.waiting.append((self.now + delay, future))
        await future

    async def settle(self):
        for _ in range(20):
            await self.yield_loop(0)

    async def advance(self, target):
        self.now = target
        for deadline, future in self.waiting:
            if deadline <= target and not future.done():
                future.set_result(None)
        await self.settle()

    async def finish(self, task):
        for _ in range(100):
            await self.settle()
            if task.done():
                return await task
            deadlines = [deadline for deadline, future in self.waiting if not future.done()]
            assert deadlines, "task stalled without a scheduled wake-up"
            await self.advance(min(deadlines))
        pytest.fail("task did not finish")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(llm.RequestPacer, "wait", paced_wait)
    monkeypatch.setattr(llm, "monotonic", lambda: clock.now)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    return clock


def coordinator(model, folder):
    evidence = [{"id": f"real-{i}", "kind": "speech", "start_seconds": i,
                 "end_seconds": i + 1, "text": "原始知识"} for i in range(7)]
    return DetailedVideoGeneration("paced", model, model.settings, folder, evidence, 10,
                          asyncio.Event(), lambda: None, lambda stage: None, {})


@pytest.mark.parametrize("failure", ["http", "network"])
@pytest.mark.parametrize("interval, expected", [(1, [0, 1, 2]), (6, [0, 6, 12]), (60, [0, 60, 120])])
def test_configured_interval_applies_to_retry_and_new_request(monkeypatch, tmp_path, clock, interval, expected, failure):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False,
                                 llm_request_interval_seconds=interval))
    sends = []
    model.diagnostics = Diagnostics(tmp_path)
    @asynccontextmanager
    async def response(self, client, payload):
        sends.append(clock.now)
        if len(sends) == 1:
            if failure == "network":
                raise httpx.ReadError("simulated stream interruption")
            yield httpx.Response(429, request=httpx.Request("POST", "https://fake.test"))
        else:
            yield httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(ModelClient, "response", response)
    async def requests():
        async with model:
            for _ in range(2):
                assert await model.request([], max_tokens=1024) == "ok"
    async def scenario():
        await clock.finish(asyncio.create_task(requests()))
    asyncio.run(scenario())
    assert sends == expected and model.calls == 3
    reports = list(tmp_path.glob("*.log"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert f'"retry_delay_seconds": {interval}' in text and f"{interval}秒后重试" in text
    assert model.client.is_closed


def test_request_interval_does_not_change_checkpoint_keys(tmp_path):
    from dataclasses import replace
    model = ModelClient(Settings())
    flow = coordinator(model, tmp_path)
    payload = outline_payload(flow.evidence)
    original = flow.path("outline", payload)
    model.settings = replace(model.settings, llm_request_interval_seconds=10)
    assert coordinator(model, tmp_path).path("outline", payload) == original


@pytest.mark.parametrize("interval", [0, 61])
def test_settings_reject_invalid_request_interval(tmp_path, interval):
    with pytest.raises(ValueError, match="1～60"):
        Settings(data_dir=tmp_path / "data", llm_request_interval_seconds=interval).prepare()


@pytest.mark.parametrize("concurrency", [1, 3, 4])
def test_new_jobs_and_retries_share_spacing_slots_checkpoints_and_usage(monkeypatch, tmp_path, clock, concurrency):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False,
                                 llm_section_concurrency=concurrency))
    flow = coordinator(model, tmp_path)
    attempts, sends, active, peak = {}, [], 0, 0

    @asynccontextmanager
    async def response(self, client, payload):
        nonlocal active, peak
        identifier = json.loads(payload["messages"][-1]["content"])["evidence"][0]["id"]
        attempts[identifier] = attempts.get(identifier, 0) + 1
        sends.append((identifier, clock.now))
        active += 1
        peak = max(peak, active)
        try:
            failed = identifier == "E2" and attempts[identifier] == 1
            await asyncio.sleep(13 if identifier == "E1" else 7 if failed else 30 if identifier == "E3" else 20)
            body = ({"error": {"code": "Throttling.Aborted", "message": "Too many requests"}} if failed else
                    {"choices": [{"message": {"content": json.dumps(outline_result(identifier))}}],
                     "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
            yield httpx.Response(429 if failed else 200, json=body, request=httpx.Request("POST", "https://fake.test"))
        finally:
            active -= 1

    monkeypatch.setattr(ModelClient, "response", response)
    jobs = [("outline", outline_payload([item])) for item in flow.evidence]

    async def scenario():
        async with model:
            task = asyncio.create_task(flow.pool(jobs))
            await clock.settle()
            for tick in (3, 6, 9, 10, 13):
                await clock.advance(tick)
            # 成功立即保存；不在成功后再固定冷却三秒。
            assert flow.path(*jobs[0]).exists()
            if concurrency == 1:
                assert sends[:2] == [("E1", 0), ("E2", 13)]
            else:
                # E2失败后的三秒和E1完成同时到达；只准其中一个先发送。
                assert len([tick for _, tick in sends if tick == 13]) == 1
                await clock.advance(16)
                assert {identifier for identifier, tick in sends if tick in {13, 16}} == {"E2", f"E{concurrency + 1}"}
            results = await clock.finish(task)
            calls = model.calls
            assert await flow.pool(jobs) == results and model.calls == calls
            return results

    results = asyncio.run(scenario())
    assert [value["video_summary"] for value in results] == [item["id"] for item in flow.evidence]
    assert all(right[1] - left[1] >= 3 for left, right in zip(sends, sends[1:]))
    assert attempts == {item["id"]: (2 if item["id"] == "E2" else 1) for item in flow.evidence}
    retry = [tick for identifier, tick in sends if identifier == "E2"]
    assert retry[1] - (retry[0] + 7) >= 3
    assert active == 0 and peak == concurrency
    assert model.calls == 8 and model.usage_reports == 7
    assert model.usage == {"prompt_tokens": 14, "completion_tokens": 7}
    assert len(list(tmp_path.glob("blocks-v4-outline-*.json"))) == 7
    assert model.client.is_closed and not model.active_requests


@pytest.mark.parametrize("stream", [False, True])
def test_all_stages_and_repairs_share_the_same_send_clock(monkeypatch, clock, stream):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=stream))
    outputs = iter([
        {},
        {"content_type": CONTENT_TYPES["课程或教材"], "chapters": [{"title": "主题", "core_question": "问题", "topics": ["内容"], "start_anchor_id": "E1"}],
         "video_summary": "视频内容"},
        {"facts": [], "issues": []},
        {"chapters": [{'chapter_id': 'C1', 'summary': '摘要', "markdown": '正文内容', "issues": []}]},
        {},  # 概览格式修正也必须经过统一间隔。
        {"title": "主题", "tags": [], "overview_paragraphs": ["整体概览"]},
        {"repairs": [{"chapter_id": "C1", "block_index": 0, "quote": "原文"}]},
    ])
    sends = []

    @asynccontextmanager
    async def response(self, client, payload):
        sends.append(clock.now)
        body = {"choices": [{"message": {"content": model_content(next(outputs))}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        if stream:
            body["choices"][0]["delta"] = body["choices"][0].pop("message")
            yield httpx.Response(200, text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
        else:
            yield httpx.Response(200, json=body)

    monkeypatch.setattr(ModelClient, "response", response)

    async def generate():
        async with model:
            for operation in ("outline", "extract", "chapters", "overview"):
                await model.generate(operation, ({"chapter": {"chapter_id": "C1"}} if operation == "extract" else {"chapters": [{"chapter_id": "C1"}]}), model_validator(operation, lambda value: None))
            result = await model.generate("issue_quotes", {"sources": [{"id": "E1", "text": "原文"}],
                "repairs": [{"chapter_id": "C1", "block_index": 0, "quote": "错误引文", "evidence_ids": ["E1"]}]}, model_validator("issue_quotes", lambda value: None))
            assert result["repairs"][0]["quote"] == "原文"

    async def scenario():
        await clock.finish(asyncio.create_task(generate()))

    asyncio.run(scenario())
    assert sends == [0, 3, 6, 9, 12, 15, 18]
    assert model.calls == model.usage_reports == 7
    assert model.usage == {"prompt_tokens": 14, "completion_tokens": 7}
    assert model.client.is_closed


@pytest.mark.parametrize("phase", ["pacing", "retry", "http"])
def test_stop_cancels_send_wait_retry_wait_and_http_without_losing_success(monkeypatch, tmp_path, clock, phase):
    model = ModelClient(Settings(llm_key="fake", free_tier_confirmed=True, llm_stream=False,
                                 llm_section_concurrency=3))
    flow = coordinator(model, tmp_path)
    model.check_stop = flow.check
    closed = []

    @asynccontextmanager
    async def response(self, client, payload):
        identifier = json.loads(payload["messages"][-1]["content"])["evidence"][0]["id"]
        try:
            if identifier == "E1":
                yield httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(outline_result(identifier))}}],
                                                "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
            elif phase == "retry":
                yield httpx.Response(429, request=httpx.Request("POST", "https://fake.test"))
            else:
                await asyncio.Event().wait()
        finally:
            closed.append(identifier)

    monkeypatch.setattr(ModelClient, "response", response)
    jobs = [("outline", outline_payload([item])) for item in flow.evidence]

    async def scenario():
        async with model:
            task = asyncio.create_task(flow.pool(jobs))
            await clock.settle()
            if phase != "pacing":
                await clock.advance(3)
            assert flow.path(*jobs[0]).exists()
            tick = time.monotonic()
            flow.stop.set()
            with pytest.raises(TaskPaused):
                await asyncio.wait_for(task, 2)
            assert time.monotonic() - tick < 2
            assert not [future for _, future in clock.waiting if not future.done()]

    asyncio.run(scenario())
    assert model.calls == (1 if phase == "pacing" else 2)
    assert model.cancelled_calls == (1 if phase == "http" else 0)
    assert model.usage_reports == 1 and model.usage == {"prompt_tokens": 2, "completion_tokens": 1}
    assert len(closed) == model.calls
    assert len(list(tmp_path.glob("blocks-v4-outline-*.json"))) == 1
    assert model.client.is_closed and not model.active_requests
