import asyncio
from copy import deepcopy

import pytest

from app.config import Settings
from app.videos.concise import ConciseVideoGeneration
from app.videos.notes import notes_markdown, notes_text
from .test_note_styles import concise_calls


def test_time_links_use_local_records_preserve_topic_order_and_code():
    from app.videos.timestamps import render_timestamps, plain_timestamps
    markdown = ("## 后讲的主题 [回看](#source-E2)\n\n解释。\n\n"
        "## 先讲的主题 [回看](#source-E1)\n\n要点。\n\n"
        "`[回看](#source-E99)`\n\n```md\n[回看](#source-E98)\n```\n")
    rendered = render_timestamps(markdown, {"E1": 2.346, "E2": 125.75})
    assert rendered.startswith("## 后讲的主题 [02:05](#video-time-125.75)")
    assert "## 先讲的主题 [00:02](#video-time-2.346)" in rendered
    assert "`[回看](#source-E99)`" in rendered and "```md\n[回看](#source-E98)\n```" in rendered
    exported = plain_timestamps(rendered)
    assert "## 后讲的主题 02:05" in exported and "## 先讲的主题 00:02" in exported
    assert "#video-time-" not in exported and "#source-E99" in exported


def test_each_heading_has_one_timestamp_without_marking_body():
    from app.videos.timestamps import render_timestamps
    markdown = ("## 基本概念 [回看](#source-E1)\n\n原理说明。\n\n"
        "### 适用条件 [回看](#source-E1)\n\n- 条件一。\n- 条件二。\n\n"
        "## 例题 [回看](#source-E2)\n\n**关键步骤**：先判断，再更新。")
    rendered = render_timestamps(markdown, {"E1": 2, "E2": 30})
    assert rendered.count("#video-time-") == 3
    assert "## 基本概念 [00:02](#video-time-2)" in rendered
    assert "### 适用条件 [00:02](#video-time-2)" in rendered
    assert "## 例题 [00:30](#video-time-30)" in rendered
    assert "- 条件一。\n- 条件二。" in rendered
    assert rendered.endswith("**关键步骤**：先判断，再更新。")


@pytest.mark.parametrize("markdown", ["没有时间标记的正文。", "说明 [回看](#source-E99)",
    "说明 [回看](#source-E2)", "说明 [00:02](#video-time-2)", "`[回看](#source-E1)`", "```md\n[回看](#source-E1)\n```",
    "说明 [回看](#source-E1)", "## 概念 [回看](#source-E1) [回看](#source-E1)",
    "## 概念 [回看](#source-E1)\n\n### 没有时间的小节\n\n解释。",
    "## 概念 [回看](#source-E1)\n\n解释 [回看](#source-E1)。"])
def test_invalid_or_code_only_time_links_are_rejected(markdown):
    from app.videos.timestamps import render_timestamps
    with pytest.raises(ValueError):
        render_timestamps(markdown, {"E1": 2.346})


@pytest.mark.parametrize("kind,seconds,label,target", [("speech", 2.346, "00:02", "2.346"), ("frame", 0.5, "00:00", "0.5")])
def test_video_timestamp_repair_and_resume_keep_compact_input_and_exports(tmp_path, concise_calls, kind, seconds, label, target):
    def response(payload, value):
        return {**value, "markdown": "## 概念 [回看](#source-E99)\n\n解释概念。" if len(concise_calls) == 1 else
                "## 概念 [回看](#source-E1)\n\n解释概念。"}
    concise_calls.hook = response
    async def run():
        from app.videos.model import ModelClient
        settings = Settings(llm_key="fake-timestamps", llm_stream=False, note_style="concise")
        evidence = [{"id": "original-long-id", "kind": kind, "text": "概念的原文解释。",
                     "start_seconds": seconds, "end_seconds": 7 if kind == "speech" else seconds}]
        original = deepcopy(evidence)
        async with ModelClient(settings) as model:
            gen = ConciseVideoGeneration("time", model, settings, tmp_path, evidence, 10, asyncio.Event(),
                lambda: None, lambda _: None, {}, lambda _: None, "课程")
            result = await gen.run()
            assert len(concise_calls) == 2
            assert f"[{label}](#video-time-{target})" in result["chapters"][0]["markdown"]
            assert concise_calls[0][1]["primary"] == [{"id": "E1", "kind": kind, "text": evidence[0]["text"]}]
            assert result["chapters"][0]["evidence_ids"] == ["original-long-id"]
            assert await gen.run() == result and len(concise_calls) == 2
            for export in (notes_text, notes_markdown):
                text = export("课程.mp4", 10, result)
                assert label in text and "#video-time-" not in text and "#source-" not in text
            from app.videos.concise import validate_result
            old = deepcopy(result)
            old["chapters"][0]["markdown"] = "已有简洁笔记正文。"
            validate_result(old, {e["id"]: e for e in evidence}, 10)
            assert "已有简洁笔记正文。" in notes_markdown("课程.mp4", 10, old)
            assert evidence == original
    asyncio.run(run())
