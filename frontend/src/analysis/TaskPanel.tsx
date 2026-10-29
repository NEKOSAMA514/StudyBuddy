import type { Task, Health } from "../types";
import { TaskProgress } from "../TaskProgress";
import { TaskActivity } from "../shared/TaskActivity";
import { stageLabel } from "./taskDisplay";
import { terminal, taskStatusLabels } from "../shared/taskStatus";

// 区分回执待确认、断线和真实任务状态，按可用状态展示停止恢复入口。
export function TaskPanel({
  task,
  health,
  busy,
  reconnecting,
  retry,
  controlTask,
  onStart,
  onReadNotes,
  syncing,
  missing,
  retryRead,
}: {
  task: Task | null;
  health: Health | null;
  busy: boolean;
  reconnecting: boolean;
  retry: () => void;
  controlTask: (action: "stop" | "resume") => void;
  onStart: () => void;
  onReadNotes: () => void;
  syncing: boolean;
  missing: boolean;
  retryRead: () => void;
}) {
  const processing = !!task && !terminal.has(task.status);
  const outdatedRecognition =
    !!task &&
    !!health?.recognition &&
    task.config?.extract_version !== health.recognition.extract_version;
  return (
    <section className="panel task-panel" id="generation-progress" tabIndex={-1}>
      <div className="panel-header">
        <h2>处理进度</h2>
        <span className={`status-pill ${task?.status || ""}`}>
          {missing ? "无法同步" : syncing ? "同步中" : task ? taskStatusLabels[task.status] : "待开始"}
        </span>
      </div>
      {syncing && !missing && <p role="status">操作已接受，正在同步状态</p>}
      {missing && <p className="notice" role="alert">任务不存在，无法更新状态。<button className="secondary" onClick={retryRead}>重新读取任务</button></p>}
      <TaskProgress progress={task?.progress} status={task?.status} />
      <TaskActivity key={task?.id || "empty"} activity={task?.activity} status={task?.status} reconnecting={reconnecting}
        stage={task ? stageLabel(task.stage, task.progress.stage_labels) : "视频已保存，等待开始处理"}
        error={task?.error} warnings={task?.warnings || []} />
      {task?.notes_state === "partial" && (
        <div className="notice" role="status">
          <p>{task.segment_progress
            ? `已完成 ${task.segment_progress.completed}/${task.segment_progress.total} 段，已生成的笔记可以先阅读。`
            : "已有部分笔记生成，可以先阅读。"}</p>
          <button className="secondary" onClick={onReadNotes}>查看已生成笔记 ↓</button>
        </div>
      )}
      {task?.config?.outline_preview && task.notes_state !== "partial" && task.notes_state !== "ready" && (
        <div className="notice" role="status">
          <p>材料导览已生成，可以先查看。</p>
          <button className="secondary" onClick={onReadNotes}>查看材料导览 ↓</button>
        </div>
      )}
      {task?.status === "stopping" && (
        <p className="muted">
          {task.stage.startsWith("transcribing") ||
              task.stage.startsWith("ocr")
              ? "正在等待当前识别结束，保存结果后停止。"
              : "正在取消请求并保存进度。"}
        </p>
      )}
      {processing && (
        <button
          className="secondary"
          disabled={busy || task?.status === "stopping"}
          onClick={() => void controlTask("stop")}
        >
          {task?.status === "stopping" ? "正在停止…" : "停止处理"}
        </button>
      )}
      {task && ["paused", "interrupted"].includes(task.status) && (
        <>
          <p className="muted">
            已保存的语音、画面识别和总结片段会复用；未完成的操作会重做。
          </p>
          <div className="task-resume-actions">
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void controlTask("resume")}
            >
              继续处理
            </button>
            {task.status === "paused" && (
              <button className="secondary" disabled={busy} onClick={onStart}>
                重新生成笔记
              </button>
            )}
          </div>
        </>
      )}
      {outdatedRecognition && (
        <p className="warning">
          这是升级前的识别结果。重新分析会使用新版识别引擎，旧版识别缓存不会被复用。
        </p>
      )}
      {task &&
        terminal.has(task.status) &&
        Object.keys(task.timings).length > 0 && (
          <div className="timings">
            {Object.entries(task.timings).map(([stage, seconds]) => (
              <span key={stage}>
                {task.progress.timing_labels[stage] || stage}{" "}
                {seconds.toFixed(1)}s
              </span>
            ))}
          </div>
        )}
      {task && task.status === "failed" && (
        <button className="secondary" disabled={busy} onClick={retry}>
          {outdatedRecognition
            ? "使用新版识别，重新分析"
            : "复用已有识别，重试任务"}
        </button>
      )}
      {(!task || task.status === "succeeded") && (
        <button className="secondary" disabled={busy} onClick={() => onStart()}>
          {task
            ? outdatedRecognition
              ? "使用新版识别，重新分析"
              : "重新生成笔记"
            : "开始处理"}
        </button>
      )}
    </section>
  );
}
