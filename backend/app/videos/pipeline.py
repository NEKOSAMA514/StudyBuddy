import asyncio
import json
import logging
import math
import threading
import time
import sys
from dataclasses import replace
from pathlib import Path
from sqlalchemy import select, update as sql_update
from ..db import Evidence, Task, Video, now
from ..errors import AppError, TaskInterrupted, TaskPaused
from .model import ModelClient
from .versions import PROMPT_VERSION, check_generation
from .detailed import DetailedVideoGeneration
from .schemas import FORMAT_VERSION, current_notes
from .notes import retain_two_notes
from ..analysis.checkpoints import CheckpointCleanup
from ..analysis.activity import TaskActivity, in_session, merge_config
from ..util.json_io import atomic_json
from ..settings.model_profiles import execution_settings
from ..recognition.media import Recognizer
from ..recognition.extraction import extraction_config, extraction_signature, recognizer_key, speech_cache_path, ocr_cache_path, frame_times, speech_signature
from .video_reader import extract_audio, extract_frame, source_path, UniformFrameReader
from .transcripts import save_transcript
from ..diagnostics import error_log_location
from ..video_download.subtitles import fetch_subtitles, subtitle_cache, content_digest, SUBTITLE_KINDS
from ..settings.network_settings import effective_proxy

logger = logging.getLogger("studybuddy")


# 执行单个视频任务，统一连接字幕、识别缓存、章节生成和状态落盘。
class Pipeline:
    recovery_message = "上次处理已中断，可手动重试；成功的识别步骤会复用。"
    def __init__(self, get_settings, sessions, diagnostics, recognizer_factory=Recognizer, model_factory=ModelClient):
        self.get_settings = get_settings
        self.sessions = sessions
        self.recognizer_factory = recognizer_factory
        self.recognizer = None
        self.recognizer_config = None
        self.model_factory = model_factory
        self.diagnostics = diagnostics
        self.checkpoint_cleanup = CheckpointCleanup(sessions, Task, get_settings().data_dir / "videos", lambda task: task.video_id, diagnostics)
        self.model_control = None
        self.subtitle_control = None
        self.bilibili_sessions = None
        self.control_lock = threading.Lock()
        self.shutdown = None

    # 写事务内确认任务状态，成功时同步保留版本并登记检查点待清理标志。
    def update(self, task_id, **values):
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, task_id)
            if "config" in values and task:
                values["config"] = merge_config(task.config, values["config"])
            statement = sql_update(Task).where(Task.id == task_id)
            if values.get("status") in {"succeeded", "failed"}:
                statement = statement.where(Task.status == "running")
            changed = session.execute(statement.values(**values, updated_at=now())).rowcount
            if values.get("status") == "succeeded" and not changed:
                raise TaskPaused()
            if (values.get("status") == "succeeded" and current_notes(values.get("result")) and
                    values["result"].get("complete", True)):
                task = session.get(Task, task_id)
                task.config = {**task.config, "checkpoint_cleanup_pending": True}
                retain_two_notes(session, task.video_id)
            if values.get("status") == "failed" and not changed:
                session.execute(sql_update(Task).where(Task.id == task_id, Task.status == "stopping")
                                .values(status="paused", error=None, updated_at=now()))
            if task:
                session.refresh(task)
                if "status" in values or "stage" in values:
                    in_session(session, task, self.diagnostics)

    def release_recognizer(self):
        self.recognizer = None
        self.recognizer_config = None

    def cleanup_checkpoints(self, task_id):
        self.checkpoint_cleanup.cleanup(task_id)

    def retry_checkpoint_cleanup(self):
        self.checkpoint_cleanup.retry()

    # 在耗时步骤之间检查用户停止和服务退出，避免继续启动下一步工作。
    def checkpoint(self, task_id):
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None or task.status in {"stopping", "paused"}:
                raise TaskPaused()
        if self.shutdown is not None and self.shutdown.is_set():
            raise TaskInterrupted()

    def finish_stop(self, task_id, warnings, timings):
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, task_id)
            if task and task.status in {"running", "stopping"}:
                paused = task.status == "stopping" or self.shutdown is None or not self.shutdown.is_set()
                task.status = "paused" if paused else "interrupted"
                task.error = None if paused else {"code": "interrupted", "message": self.recovery_message}
                task.warnings, task.timings, task.updated_at = warnings, timings, now()
                in_session(session, task, self.diagnostics)

    def cancel_model(self, task_id):
        with self.control_lock:
            if self.subtitle_control and self.subtitle_control[0] == task_id:
                self.subtitle_control[1].set()
            control = self.model_control
            if control and control[0] == task_id:
                control[1].call_soon_threadsafe(control[2].set)

    # 优先复用任务绑定的字幕；首次平台字幕不可用时记录原因并回退本地识别。
    def read_subtitles(self, task_id, config, settings, folder, duration, warnings, activity=lambda *a, **k: None):
        source = config.get("transcript_source", {})
        if source.get("kind") in SUBTITLE_KINDS:
            path = subtitle_cache(folder, source)
            if not path.is_file():
                raise AppError("file_missing", "本任务使用的字幕缓存已丢失，请重新生成笔记。")
            speech = json.loads(path.read_text(encoding="utf-8"))
            if content_digest(speech) != source["digest"]:
                raise AppError("invalid_media", "本任务的字幕缓存已改变，请重新生成笔记。")
            activity("cached", message="复用已保存的平台字幕")
            return speech
        platform = config.get("video_source", {}).get("platform")
        enabled = platform == "youtube" or platform == "bilibili" and config.get("subtitle_policy") == "platform-first-v2"
        if source.get("kind") == "asr" or not config.get("subtitle_policy") or not enabled:
            if config.get("subtitle_policy") and not source:
                config["transcript_source"] = {"kind": "asr", "language": settings.asr_language, "automatic": True}
            if source.get("fallback_reason"):
                warnings.append("平台字幕不可用，本次已改用语音识别。")
            return None
        stopped = threading.Event()
        with self.control_lock:
            self.subtitle_control = (task_id, stopped)
        try:
            self.checkpoint(task_id)
            activity("preparing", message="正在读取平台字幕")
            try:
                options = {}
                if platform == "bilibili":
                    options["cookies"] = self.bilibili_sessions.cookies() if self.bilibili_sessions else {}
                result = fetch_subtitles(config["video_source"], settings.asr_language, duration,
                                         folder, effective_proxy(settings), stopped, **options)
            except Exception as exc:
                self.checkpoint(task_id)
                self.diagnostics.record("subtitle_fetch_failed", "平台字幕获取失败，改用语音识别。", exc, task_id=task_id)
                result = {"speech": [], "reason": "fetch_failed"}
            self.checkpoint(task_id)
            if result["speech"]:
                atomic_json(subtitle_cache(folder, result["source"]), result["speech"])
                config["transcript_source"] = result["source"]
                activity("saved", message="平台字幕已获取并保存")
                return result["speech"]
            config["transcript_source"] = {"kind": "asr", "language": settings.asr_language,
                                           "automatic": True, "fallback_reason": result["reason"]}
            reasons = {"credentials_missing": "未登录 B 站，平台字幕不可用",
                       "credentials_expired": "B 站登录已失效，平台字幕不可用",
                       "network_failed": "平台字幕网络读取失败"}
            warnings.append(reasons.get(result["reason"], "平台字幕不可用") + "，本次已改用语音识别。")
            activity("preparing", message=warnings[-1], level="warning")
            logger.info("task=%s subtitle_fallback reason=%s formats=%s", task_id, result["reason"], result.get("failures", []))
            return None
        finally:
            with self.control_lock:
                self.subtitle_control = None

    # 领取任务后按原执行快照复用或补做识别，再运行对应笔记风格的生成器。
    def run(self, task_id, *, shutdown=None):
        self.shutdown = shutdown
        settings = self.get_settings()
        warnings, timings = [], {}
        claimed = False
        stage = "checking"
        total = time.monotonic()
        tick = total
        try:
            if shutdown is not None and shutdown.is_set():
                return
            # 先检查版本，再领取任务、执行识别或更新配置。
            with self.sessions.begin() as session:
                task = session.get(Task, task_id)
                if not task or task.status != "queued":
                    return
                try:
                    check_generation(task.config)
                except AppError as exc:
                    task.status, task.error, task.updated_at = "failed", {"code": exc.code, "message": exc.message}, now()
                    return
                claimed = session.execute(sql_update(Task).where(Task.id == task_id, Task.status == "queued")
                    .values(status="running", error=None, updated_at=now())).rowcount
            if not claimed:
                return
            with self.sessions() as session:
                task = session.get(Task, task_id)
                video = session.get(Video, task.video_id)
            activity = TaskActivity(self.sessions, Task, task_id, task.attempts, self.diagnostics)
            activity("preparing", message="检查视频材料" if task.attempts == 1 else "继续处理，检查可复用材料")
            self.checkpoint(task_id)
            settings = execution_settings(settings, task.config)
            fields = ("asr_model", "asr_engine", "asr_language", "ocr_engine", "ocr_languages", "ocr_min_confidence",
                      "frame_interval")
            settings = replace(settings, **{key: task.config[key] for key in fields if key in task.config})
            key = recognizer_key(settings)
            if self.recognizer is None or self.recognizer_config != key:
                self.recognizer = self.recognizer_factory(settings)
                self.recognizer_config = key
            recognizer = self.recognizer
            recognizer.cancel = shutdown
            recognizer.check_stop = lambda: self.checkpoint(task_id)
            extraction = extraction_config(settings, task.config)
            config = {**task.config, "execution_started": True}
            self.update(task_id, config=config)
            self.checkpoint(task_id)
            folder = settings.data_dir / "videos" / video.id
            media = source_path(folder, video.media_info)
            recognition_signature = extraction_signature({key: value for key, value in config.items() if key != "transcript_source"})
            evidence = []
            stage = "transcribing"
            self.update(task_id, stage=stage)
            tick = time.monotonic()
            speech = self.read_subtitles(task_id, config, settings, folder, video.duration, warnings, activity)
            self.update(task_id, config=dict(config))
            self.checkpoint(task_id)
            signature = extraction_signature(config)
            speech_cache = speech_cache_path(folder, speech_signature(config))
            if speech is None and video.audio_codec:
                if speech_cache.exists():
                    speech = json.loads(speech_cache.read_text(encoding="utf-8"))
                    activity("cached", message="复用已完成的语音识别结果")
                else:
                    try:
                        audio = folder / "audio.wav"
                        if not audio.exists():
                            activity("preparing", message="正在提取讲解音频")
                            extract_audio(media, audio, video.media_info, **({"cancel": shutdown} if shutdown is not None else {}))
                        activity("preparing", message="正在识别讲解语音，等待当前推理完成")
                        speech = recognizer.transcribe(audio)
                        atomic_json(speech_cache, speech)
                        self.checkpoint(task_id)
                    except (TaskInterrupted, TaskPaused):
                        raise
                    except Exception as exc:
                        self.checkpoint(task_id)
                        reference = self.diagnostics.record("asr_failed", "语音识别失败，继续 OCR。", exc, task_id=task_id, stage=stage)
                        activity("preparing", message="语音识别失败，继续使用画面文字", level="warning", error=reference)
                        logger.warning("task=%s stage=transcribing failed; OCR continues", task_id)
                        speech = []
                        log_message = error_log_location(reference)
                        warnings.append(f"语音识别未完成，当前只使用画面文字。{log_message}重试时会再次尝试识别。")
            elif speech is None:
                speech = []
                warnings.append("视频没有音轨，本次仅依据抽样画面文字。")
            if speech:
                for index, item in enumerate(speech):
                    start, end = float(item["start_seconds"]), float(item["end_seconds"])
                    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or start >= video.duration:
                        warnings.append("有一条语音时间范围异常，已排除。")
                        continue
                    evidence.append({"id": f"{video.id}-{signature}-speech-{index}", "kind": "speech",
                                     "start_seconds": start, "end_seconds": min(end, video.duration),
                                     "text": item["text"], "image_name": None, "confidence": None})
            timings[stage] = round(time.monotonic() - tick, 3)
            self.persist_evidence(video.id, evidence)
            save_transcript(folder, signature, evidence, filename=video.filename, duration=video.duration,
                            transcript_source=config.get("transcript_source"))
            self.checkpoint(task_id)

            stage = "ocr"
            self.update(task_id, stage=stage, warnings=warnings, timings=timings)
            tick = time.monotonic()
            failures = 0
            cache_hits = 0
            ocr_log = None
            positions = frame_times(config, video.duration)
            reader = UniformFrameReader(media, video.media_info) if "frame_count" in config else None
            try:
                for index, seconds in enumerate(positions):
                    self.checkpoint(task_id)
                    image_name = f"frame-{recognition_signature}-{index:05d}.jpg"
                    image = folder / image_name
                    cache = ocr_cache_path(folder, recognition_signature, index)
                    item = None
                    try:
                        if cache.exists():
                            item = json.loads(cache.read_text(encoding="utf-8"))
                            cache_hits += 1
                        else:
                            if not image.exists():
                                if reader:
                                    reader.save(image, seconds, **({"cancel": shutdown} if shutdown is not None else {}))
                                else:
                                    extract_frame(media, image, seconds, video.media_info, **({"cancel": shutdown} if shutdown is not None else {}))
                            text, confidence = recognizer.ocr(image)
                            item = {"id": f"{video.id}-{signature}-frame-{index}", "kind": "frame",
                                    "start_seconds": seconds, "end_seconds": seconds, "text": text,
                                    "image_name": image_name, "confidence": confidence}
                            atomic_json(cache, item)
                        item = {**item, "id": f"{video.id}-{signature}-frame-{index}"}
                        if item["text"].strip():
                            evidence.append(item)
                    except (TaskInterrupted, TaskPaused):
                        raise
                    except Exception as exc:
                        self.checkpoint(task_id)
                        item = None
                        failures += 1
                        ocr_log = self.diagnostics.record("ocr_failed", "画面识别失败。", exc, task_id=task_id, stage=stage, frame=index, seconds=seconds)
                    self.complete_frame(task_id, video.id, item if item and item["text"].strip() else None,
                                        f"ocr:{index + 1}/{len(positions)}")
            finally:
                original_error = sys.exc_info()[0] is not None
                if reader:
                    try:
                        reader.close()
                    except Exception as exc:
                        if not original_error:
                            raise
                        self.diagnostics.record("frame_reader_close_failed", "解码器清理失败，保留原始退出原因。", exc, task_id=task_id)
                try:
                    save_transcript(folder, signature, evidence, filename=video.filename, duration=video.duration,
                                    transcript_source=config.get("transcript_source"))
                except Exception as exc:
                    if not original_error:
                        raise
                    self.diagnostics.record("transcript_save_failed", "停止或失败时保存转录未完成，识别缓存已保留。", exc, task_id=task_id)
            if failures:
                log_message = error_log_location(ocr_log)
                warnings.append(f"有 {failures} 张画面未能完成识别，其余结果已保留。{log_message}")
                activity("preparing", message=f"有 {failures} 张画面识别失败，其余结果已保留", level="warning", error=ocr_log)
            if cache_hits:
                activity("cached", message=f"复用 {cache_hits} 张画面的文字识别结果")
            timings[stage] = round(time.monotonic() - tick, 3)
            evidence.sort(key=lambda e: (e["start_seconds"], e["kind"]))
            if not evidence:
                raise AppError("no_evidence", "没有识别到有效语音或画面文字，不能生成有依据的总结。")
            kinds = {e["kind"] for e in evidence}
            if kinds == {"speech"}:
                warnings.append("没有提取到有效画面文字，本次总结仅依据讲解文本。")
            elif kinds == {"frame"} and video.audio_codec:
                warnings.append("本次总结仅依据抽样画面文字，不能代表未识别的语音内容。")
            def result_values(notes, usage, calls):
                return {**notes, "recognition": {**extraction,
                        **({"frame_interval": video.duration / config["frame_count"]} if "frame_count" in config else {}),
                        **({"transcript_source": config["transcript_source"]} if "transcript_source" in config else {})},
                        "evidence_ids": notes.get("evidence_ids", [e["id"] for e in evidence]),
                        "model": settings.llm_model, "prompt_version": PROMPT_VERSION,
                        "usage": usage, "model_requests": config.get("request_count", calls), "basis": sorted(kinds),
                        "coverage_note": "各片段优先依据带时间的讲解文本定位，无讲解文本时使用画面采样时间。自动定位为近似值，引用有效不等于内容一定正确，请核对原文。"}

            async def generate_notes():
                nonlocal stage
                loop, stopped = asyncio.get_running_loop(), asyncio.Event()
                with self.control_lock:
                    self.model_control = (task_id, loop, stopped)
                try:
                    self.checkpoint(task_id)
                    async with self.model_factory(settings) as model:
                        model.diagnostics = self.diagnostics
                        model.activity = activity
                        model.check_stop = lambda: self.checkpoint(task_id)
                        def reserve_request():
                            self.checkpoint(task_id)
                            config["request_count"] = config.get("request_count", 0) + 1
                            self.update(task_id, config=dict(config))
                        model.before_request = reserve_request
                        def publish_outline(summary):
                            config["outline_preview"] = summary
                            self.update(task_id, config=dict(config))

                        def update_segment(progress):
                            old = config.get("segment_progress", {})
                            if old.get("generation_key") == progress["generation_key"]:
                                phases = {"planning": 0, "summarizing": 1, "issues": 2, "completed": 3, "overview": 4}
                                position = lambda item: (item["completed"], item["current"], phases[item["phase"]])
                                if position(old) > position(progress):
                                    return
                            if old.get("current") == progress["current"] and old.get("phase") == progress["phase"]:
                                progress = {**old, **progress}
                            config.update(segmented=True, segment_progress=progress)
                            self.update(task_id, config=dict(config))

                        def publish_notes(notes):
                            with self.sessions.begin() as session:
                                current = session.get(Task, task_id)
                                old = current.result or {}
                                if old.get("generation_key") == notes["generation_key"]:
                                    covered = old.get("covered_until_seconds", -1)
                                    if (settings.note_style == "concise" and old.get("completed_segments", 0) >= notes["completed_segments"]):
                                        return
                                    if settings.note_style != "concise" and (covered > notes["covered_until_seconds"] or
                                            covered == notes["covered_until_seconds"] and
                                            old.get("segment_guides", []) == notes.get("segment_guides", [])):
                                        return
                                snapshot = result_values(notes, dict(model.usage), model.calls)
                                snapshot["title"] = notes["title"] if settings.note_style == "concise" else video.filename
                                snapshot["notes_revision"] = old.get("notes_revision", 0) + 1
                                session.execute(sql_update(Task).where(Task.id == task_id, Task.status.in_(["running", "stopping"]))
                                                .values(result=snapshot, updated_at=now()))
                            activity("saved", message="本段笔记已校验并保存")

                        def update_model_stage(value):
                            nonlocal stage
                            stage = value.split(":", 1)[0]
                            if config.get("segmented") and stage in {"planning", "summarizing"}:
                                if stage == "summarizing" and ":" in value:
                                    done, total = map(int, value.split(":", 1)[1].split("/"))
                                    saved = config["segment_progress"]
                                    if saved.get("chapter_total") == total:
                                        done = max(done, saved.get("chapter_processed", 0))
                                    config["segment_progress"] = {**config["segment_progress"],
                                                                  "chapter_processed": done, "chapter_total": total}
                                stage, value = "segments", "segments"
                                self.update(task_id, config=dict(config))
                            self.update(task_id, stage=value, warnings=warnings, timings=timings)
                        if settings.note_style == "concise":
                            from .concise import ConciseVideoGeneration
                            generation = ConciseVideoGeneration(task_id, model, settings, folder, evidence, video.duration,
                                stopped, lambda: self.checkpoint(task_id), update_model_stage, timings, publish_notes,
                                config["video_title"])
                        else:
                            generation = DetailedVideoGeneration(task_id, model, settings, folder, evidence, video.duration,
                                                        stopped, lambda: self.checkpoint(task_id), update_model_stage, timings,
                                                        publish=publish_notes, segment_update=update_segment,
                                                        video_title=config["video_title"], outline_ready=publish_outline)
                        active, stopping = asyncio.create_task(generation.run()), asyncio.create_task(stopped.wait())
                        try:
                            await asyncio.wait([active, stopping], return_when=asyncio.FIRST_COMPLETED)
                            if stopped.is_set():
                                self.checkpoint(task_id)
                                raise TaskPaused()
                            return await active, model.usage, model.calls
                        finally:
                            active.cancel()
                            stopping.cancel()
                            await asyncio.gather(active, stopping, return_exceptions=True)
                            logger.info("task=%s model_requests=%s cancelled=%s usage_reports=%s usage=%s usage_unknown=%s",
                                        task_id, model.calls, model.cancelled_calls, model.usage_reports,
                                        model.usage, max(0, model.calls - model.usage_reports))
                finally:
                    with self.control_lock:
                        self.model_control = None

            notes, usage, calls = asyncio.run(generate_notes())
            self.checkpoint(task_id)
            timings["total"] = round(time.monotonic() - total, 3)
            result = result_values(notes, usage, calls)
            with self.sessions() as session:
                result["notes_revision"] = (session.get(Task, task_id).result or {}).get("notes_revision", 0) + 1
            self.update(task_id, status="succeeded", stage="completed", result=result, warnings=warnings, timings=timings)
            self.cleanup_checkpoints(task_id)
            logger.info("task=%s status=succeeded duration=%.2f", task_id, timings["total"])
        except (TaskPaused, TaskInterrupted):
            timings["total"] = round(time.monotonic() - total, 3)
            self.finish_stop(task_id, warnings, timings)
        except Exception as exc:
            with self.sessions() as session:
                stopping = session.get(Task, task_id).status == "stopping"
            if stopping:
                reference = self.diagnostics.record("stopped_operation_failed", "停止前的当前操作未能完成，继续时会重做。", exc, task_id=task_id, stage=stage)
                message = "停止前的当前操作未能完成，继续时会重做。"
                message += f"详细日志：{reference['log_file']}（错误编号 {reference['error_id']}）。" if reference.get("log_file") else "日志未能写入，请检查 log 文件夹权限。"
                self.update(task_id, status="paused", error=None, warnings=[*warnings, message], timings=timings)
                return
            error = exc if isinstance(exc, AppError) else AppError("processing_failed", "处理失败；已保存的证据不会删除，请检查环境后重试。")
            reference = self.diagnostics.record(error.code, error.message, exc, task_id=task_id, stage=stage,
                                                asr_engine=settings.asr_engine, ocr_engine=settings.ocr_engine, llm_model=settings.llm_model)
            timings["total"] = round(time.monotonic() - total, 3)
            self.update(task_id, status="failed", error={"code": error.code, "message": error.message, **reference}, warnings=warnings, timings=timings)
            logger.warning("task=%s status=failed stage=%s code=%s", task_id, stage, error.code)
        finally:
            if claimed:
                if stage in {"transcribing", "ocr"} and stage not in timings:
                    timings[stage] = round(time.monotonic() - tick, 3)
                timings["total"] = round(time.monotonic() - total, 3)
                try:
                    with self.sessions.begin() as session:
                        task = session.get(Task, task_id)
                        if task:
                            task.timings = dict(timings)
                except Exception as exc:
                    self.diagnostics.record("task_timings_save_failed", "执行耗时保存失败，保留任务原有状态和退出原因。", exc, task_id=task_id)
            self.shutdown = None

    # 每帧证据与 OCR 进度一并提交，取消或失败后可从已确认位置接续。
    def complete_frame(self, task_id, video_id, item, stage):
        with self.sessions.begin() as session:
            # 先取得写锁，再读取状态，避免读取快照后升级写事务。
            session.execute(sql_update(Task).where(Task.id == task_id).values(stage=stage, updated_at=now()))
            if item and not session.get(Evidence, item["id"]):
                session.add(Evidence(video_id=video_id, **item))
            task = session.get(Task, task_id)
            stopped = task is None or task.status in {"stopping", "paused"}
        if stopped:
            raise TaskPaused()
        self.checkpoint(task_id)

    def persist_evidence(self, video_id, evidence):
        with self.sessions.begin() as session:
            for item in evidence:
                if not session.get(Evidence, item["id"]):
                    session.add(Evidence(video_id=video_id, **item))
