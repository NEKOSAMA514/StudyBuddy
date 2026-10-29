from ..db import Task, Video
from ..api_schemas import TaskPublic, VideoPublic, EvidencePublic
from .schemas import current_notes
STEPS = (
    {"key": "checking", "title": "检查视频", "description": "检查格式、时长和本地原件"},
    {"key": "transcribing", "title": "识别讲解语音", "description": "提取带时间信息的转录原文"},
    {"key": "ocr", "title": "提取画面文字", "description": "按视频时长均匀采样，识别画面文字"},
    {"key": "planning", "title": "规划视频章节", "description": "识别主题、核心问题和证据边界"},
    {"key": "summarizing", "title": "生成章节正文", "description": "按章写作，关联原始依据"},
    {"key": "finalizing", "title": "整理与校验", "description": "生成整体概览，检查引用与时间"},
)
SEGMENT_STEPS = (*STEPS[:3],
    {"key": "segments", "title": "逐段生成笔记", "description": "按片段规划与写作，完成一段即可阅读"},
    {"key": "finalizing", "title": "生成整体概览", "description": "汇总片段摘要，生成标题、标签与概览"})
STAGE_LABELS = {"queued": "等待处理", "checking": "检查视频", "transcribing": "识别讲解语音",
                "ocr": "提取画面文字", "planning": "规划视频章节", "summarizing": "生成章节正文",
                "segments": "逐段生成笔记", "finalizing": "生成整体概览与校验引用", "completed": "分析完成"}
TIMING_LABELS = {"transcribing": "识别讲解语音", "ocr": "提取画面文字", "planning": "规划视频章节",
                 "summarizing": "生成章节正文", "finalizing": "生成整体概览与校验引用", "total": "本次总耗时"}


# 将阶段、计数和任务状态转换为前端统一使用的进度结构。
def task_progress(task):
    """步骤完成度，不是预计耗时百分比。没有细粒度回调时不编造进度。"""
    segmented = task.config.get("segmented", False)
    segment = task.config.get("segment_progress")
    steps = SEGMENT_STEPS if segmented else STEPS
    concise = task.config.get("note_style") == "concise"
    if concise:
        steps = (*STEPS[:3], {"key": "concise", "title": "生成简洁笔记", "description": "按输入预算逐段生成短文"})
    stages = [step["key"] for step in steps]
    stage, _, detail = task.stage.partition(":")
    completed = len(stages) if task.status == "succeeded" else stages.index(stage) if stage in stages else 0
    done, total = 0, None
    if stage in {"ocr", "planning", "summarizing", "concise"} and "/" in detail:
        try:
            done, total = (int(value) for value in detail.split("/"))
            if total <= 0 or not 0 <= done <= total:
                done, total = 0, None
        except ValueError:
            done, total = 0, None
    fraction = done / total if total else 0
    if segmented and segment and (stage in {"segments", "queued"} or
            stage in {"checking", "transcribing", "ocr"} and current_notes(task.result)):
        completed = 3
        phase = segment["phase"]
        inner = (0.25 + 0.7 * segment.get("chapter_processed", 0) / max(1, segment.get("chapter_total", 1))
                 if phase == "summarizing" else 0.95 if phase == "issues" else 0)
        fraction = min(1, (segment["completed"] + inner) / segment["total"])
        done, total = segment["completed"], segment["total"]
    percent = min(100 if task.status == "succeeded" else 99, int(100 / len(stages) * (completed + fraction)))
    stage_labels, timing_labels = dict(STAGE_LABELS), dict(TIMING_LABELS)
    if concise:
        stage_labels["concise"] = "生成简洁笔记"
        timing_labels = {key: value for key, value in timing_labels.items() if key not in {"planning", "summarizing", "finalizing"}}
        timing_labels["concise"] = "生成简洁笔记"
    source = task.config.get("transcript_source", {})
    if source.get("kind") in {"youtube_subtitle", "bilibili_subtitle"}:
        label = "平台自动字幕" if source.get("automatic") else "平台字幕"
        steps = tuple({**step, "title": f"读取{label}", "description": "读取带时间信息的视频字幕"}
                      if step["key"] == "transcribing" else step for step in steps)
        stage_labels["transcribing"] = timing_labels["transcribing"] = f"读取{label}"
    elif (task.config.get("subtitle_policy") and not source
          and (task.config.get("video_source", {}).get("platform") != "bilibili"
               or task.config.get("subtitle_policy") == "platform-first-v2")):
        stage_labels["transcribing"] = "尝试获取平台字幕"
    return {"percent": percent, "completed_steps": completed, "current_step": min(completed + 1, len(stages)),
            "processed": done, "total": total, "segmented": segmented, "segment": segment,
            "steps": list(steps), "stage_labels": stage_labels, "timing_labels": timing_labels}


# 按公开契约生成任务摘要，正文、内部配置与敏感字段不直接回传。
def public(row):
    if isinstance(row, Task):
        value = {name: getattr(row, name) for name in TaskPublic.model_fields
                 if name not in {"progress", "notes_state", "notes_revision", "segment_progress", "note_style", "activity"}}
        from ..analysis.activity import public_activity
        value["activity"] = public_activity(row)
        value["config"] = {key: row.config[key] for key in ("extract_version", "outline_preview", "transcript_source") if key in row.config}
        value["progress"] = task_progress(row)
        value["notes_state"] = ("ready" if row.result.get("complete", True) else "partial") if current_notes(row.result) else "none"
        value["notes_revision"] = (row.result or {}).get("notes_revision", 0)
        value["segment_progress"] = row.config.get("segment_progress")
        value["note_style"] = row.config.get("note_style", "detailed")
        return TaskPublic.model_validate(value).model_dump()
    schema = VideoPublic if isinstance(row, Video) else EvidencePublic
    return schema.model_validate(row).model_dump()
