"""有容量限制且尽力记录的任务动态，不进入生成快照或模型输入。"""
import copy
import json
import logging
from datetime import datetime, timezone
from ..db import now

logger = logging.getLogger("studybuddy")
RUNTIME_KEY = "task_activity"
OPERATIONS = {"outline": "大纲", "extract": "章节信息", "chapters": "章节正文",
              "overview": "概览", "issue_quotes": "引文修正", "concise": "简洁导览"}
STATES = {"queued": "排队中", "preparing": "准备材料", "pacing": "等待发送",
          "waiting": "等待模型返回", "receiving": "接收结果", "validating": "校验结果",
          "validated": "校验通过，等待保存", "saving": "保存结果", "saved": "结果已保存",
          "cached": "复用已完成结果", "retry_wait": "自动重试等待", "repair": "结构修正",
          "stopping": "正在停止", "succeeded": "处理完成", "paused": "已停止",
          "interrupted": "任务已中断", "failed": "处理失败"}
STAGES = {"checking": "检查材料", "transcribing": "读取字幕或识别讲解语音", "ocr": "提取画面文字",
          "planning": "规划章节", "summarizing": "生成章节正文", "finalizing": "整理概览与校验",
          "concise": "生成简洁导览", "segments": "按片段生成笔记"}
TERMINAL = {"succeeded", "failed", "paused", "interrupted"}


def report_failure(diagnostics, exc):
    # 诊断记录失败也不能进入模型的重试或回退流程。
    try:
        if diagnostics:
            diagnostics.record("task_activity_failed", "任务动态未能更新，生成流程继续。", exc)
    except Exception:
        logger.warning("task_activity_failed diagnostic_unavailable=true")


def emit(model, state, **details):
    try:
        callback = getattr(model, "activity", None)
        if callback:
            callback(state, **details)
    except Exception as exc:
        report_failure(getattr(model, "diagnostics", None), exc)


def merge_config(current, incoming):
    """业务快照即使带有旧动态，也不能覆盖后来记录的新动态。"""
    value = {k: v for k, v in incoming.items() if k != RUNTIME_KEY}
    if RUNTIME_KEY in current:
        value[RUNTIME_KEY] = current[RUNTIME_KEY]
    return value


def clean(text, diagnostics):
    text = str(text or "")
    if diagnostics:
        text = diagnostics.redact(text)
    return text[:300]


def apply(task, state, diagnostics=None, **details):
    runtime = copy.deepcopy(task.config.get(RUNTIME_KEY) or {})
    epoch = task.attempts
    if runtime.get("attempt") != epoch:
        runtime = {"attempt": epoch, "sequence": runtime.get("sequence", 0),
                   "history": runtime.get("history", []), "dropped": runtime.get("dropped", 0), "requests": {}}
    runtime.setdefault("requests", {})
    request_id = details.get("request_id")
    if state == "request_done":
        runtime["requests"].pop(request_id, None)
        return runtime
    stamp = now()
    operation = details.get("operation", "")
    label = OPERATIONS.get(operation, "结果")
    message = details.get("message") or (f"{STATES[state]} · {label}" if operation else STATES[state])
    entry = {"at": stamp, "state": state, "message": clean(message, diagnostics),
             "level": details.get("level", "info"), "attempt": epoch}
    if request_id:
        entry.update({key: details[key] for key in ("request_id", "operation", "network_attempt", "repair", "chapter_ids") if key in details})
    reference = details.get("error")
    if reference:
        entry["error"] = {key: clean(reference[key], diagnostics) for key in
                          ("code", "message", "error_id", "log_file") if reference.get(key)}
        if state in {"failed", "interrupted"}:
            entry["level"] = "error"
            entry["message"] = clean(f"{STATES[state]}：{reference.get('message', '请查看错误日志。')}", diagnostics)
    if request_id:
        previous = runtime["requests"].get(request_id, {})
        request = {"id": request_id, "operation": operation, "state": state,
                   "attempt": epoch, "network_attempt": details.get("network_attempt", 1),
                   "repair": details.get("repair", 0), "chapter_ids": list(details.get("chapter_ids", [])),
                   "state_since": stamp, "sent_at": previous.get("sent_at")}
        if state == "waiting":
            request["sent_at"] = stamp
        runtime["requests"][request_id] = request
    runtime["current"] = entry
    if state in TERMINAL or state == "queued":
        runtime["requests"] = {}
        runtime["final_error"] = entry.get("error") if state in {"failed", "interrupted"} else None
    history = runtime.setdefault("history", [])
    # OCR 或章节计数只更新动态快照，避免进度变化反复刷日志。
    if not history or any(history[-1].get(k) != entry.get(k) for k in ("state", "message", "attempt", "error", "request_id", "network_attempt", "repair")):
        runtime["sequence"] = runtime.get("sequence", 0) + 1
        history.append({**entry, "id": runtime["sequence"]})
        if len(history) > 100:
            runtime["dropped"] = runtime.get("dropped", 0) + len(history) - 100
            runtime["history"] = history[-100:]
    return runtime


def in_session(session, task, diagnostics, state=None, **details):
    """用保存点隔离动态写入失败，避免影响业务或终态事务。"""
    try:
        state = state or (task.status if task.status != "running" else "preparing")
        if state == "preparing":
            details.setdefault("message", STAGES.get(task.stage.split(":")[0], "准备材料"))
        details.setdefault("error", task.error)
        value = apply(task, state, diagnostics, **details)
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        with session.begin_nested():
            task.config = {**task.config, RUNTIME_KEY: value}
            session.flush()
    except Exception as exc:
        report_failure(diagnostics, exc)


# 只向当前尝试且仍在运行的任务写入动态，旧请求不能覆盖恢复后的状态。
class TaskActivity:
    def __init__(self, sessions, table, identifier, attempt, diagnostics):
        self.sessions, self.table, self.identifier = sessions, table, identifier
        self.attempt, self.diagnostics = attempt, diagnostics

    def __call__(self, state, **details):
        try:
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = session.get(self.table, self.identifier)
                if task is None or task.attempts != self.attempt or task.status != "running":
                    return
                value = apply(task, state, self.diagnostics, **details)
                json.dumps(value, ensure_ascii=False, allow_nan=False)
                task.config = {**task.config, RUNTIME_KEY: value}
        except Exception as exc:
            report_failure(self.diagnostics, exc)


# 按公开字段构造有限历史和当前请求状态，耗时计算不回写任务更新时间。
def public_activity(task):
    runtime = task.config.get(RUNTIME_KEY)
    if not runtime:
        return None
    def event(value):
        return {k: value[k] for k in ("id", "at", "state", "message", "level", "attempt", "error",
                                     "request_id", "operation", "network_attempt", "repair", "chapter_ids") if k in value}
    active = task.status == "running" and runtime.get("attempt") == task.attempts
    requests = []
    for request in runtime.get("requests", {}).values() if active else []:
        value = {k: request.get(k) for k in ("id", "operation", "state", "attempt", "network_attempt", "repair",
                                            "chapter_ids", "state_since", "sent_at")}
        start = request.get("sent_at") if request["state"] in {"waiting", "receiving"} else request.get("state_since")
        value["elapsed_seconds"] = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(start)).total_seconds()) if start else 0
        requests.append(value)
    return {"attempt": runtime["attempt"], "sequence": runtime.get("sequence", 0),
            "history": [event(e) for e in runtime.get("history", [])], "dropped": runtime.get("dropped", 0),
            "current": event(runtime["current"]) if runtime.get("current") else None,
            "requests": requests, "final_error": runtime.get("final_error")}
