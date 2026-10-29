import type { AnalysisSelection } from "../types";
import { useEffect, useRef, useState } from "react";
import type { Health, Task, TaskReceipt, Video } from "../types";
import { clock } from "../util/time";
import { VideoPlayer } from "../VideoPlayer";
import {
  AnalysisSetupDialog,
} from "../settings/AnalysisSetupDialog";
import { useAnalysisTask } from "./useAnalysisTask";
import { useAnalysisContent } from "./useAnalysisContent";
import { useNoteVersions } from "../notes/useNoteVersions";
import { terminal } from "../shared/taskStatus";
import { TaskPanel } from "./TaskPanel";
import { Notes } from "./Notes";
import { Transcript } from "./Transcript";
import type { NoteTarget } from "../navigation";
import { useNoteTarget } from "../notes/useNoteTarget";

const versionTime = (task: Task) => task.updated_at;

// 组合任务、笔记版本和播放器，当前执行任务与正在阅读的版本分别维护。
export function VideoDetail({
  video,
  health,
  blocked,
  uploadPercent,
  onUpload,
  onBack,
  onTaskChanged,
  refresh,
  onError,
  onBusyChange,
  settingsOpen,
  target,
  receipt,
  onTaken,
}: {
  video: Video;
  health: Health | null;
  blocked: boolean;
  uploadPercent: number | null;
  onUpload: (files: FileList | null) => Promise<void>;
  onBack: () => void;
  onTaskChanged: (task: Task) => void;
  refresh: () => Promise<unknown>;
  onError: (error: unknown) => void;
  onBusyChange: (busy: boolean) => void;
  settingsOpen: boolean;
  target?: NoteTarget;
  receipt?: Pick<TaskReceipt, "task_id">;
  onTaken?: () => void;
}) {
  const analysis = useAnalysisTask({
    video,
    blocked,
    onTaskChanged,
    refresh,
    onError,
    onBusyChange,
    receipt,
    onTaken,
  });
  const { task } = analysis;
  const notes = useNoteVersions({ scope: `video/${video.id}`, path: `/videos/${video.id}/analyses`, current: task, timestamp: versionTime, onError,
    requestedId: target?.taskId, navigationKey: JSON.stringify(target) });
  const readingTask = notes.task;
  const { result, reading, evidence, availableEvidence, evidenceReading } = useAnalysisContent(
    video.id,
    readingTask,
    onError,
  );
  const [setupOpen, setSetupOpen] = useState(false);
  const targetNotice = useNoteTarget(target, readingTask?.id, result, reading.loading || notes.loading,
    notes.requestedUnavailable, "note-block", "video-notes");
  const [noteReturnTarget, setNoteReturnTarget] = useState<string | null>(null);
  const [jumpTime, setJumpTime] = useState<number | null>(null);
  const player = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const busy = blocked || analysis.busy;
  const processing = !!readingTask && readingTask.id === task?.id && !terminal.has(task.status);
  useEffect(() => {
    setNoteReturnTarget(null);
  }, [readingTask?.id]);
  useEffect(() => {
    if (settingsOpen) setSetupOpen(false);
  }, [settingsOpen]);
  // 只定位视频并记录返回笔记位置，不因点击时间戳滚动整页。
  function jump(seconds: number, noteId = "video-notes") {
    if (!player.current) return;
    if (player.current.readyState >= 1) player.current.currentTime = seconds;
    setJumpTime(seconds);
    setNoteReturnTarget(noteId);
  }
  function returnToNotes() {
    const target = document.getElementById(noteReturnTarget || "video-notes");
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.focus({ preventScroll: true });
  }
  function readCurrentNotes() {
    if (task) notes.select(task.id);
    setNoteReturnTarget(null);
    requestAnimationFrame(() => {
      const target = document.getElementById("video-notes");
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
      target?.focus({ preventScroll: true });
    });
  }
  function returnToGeneration() {
    if (task) notes.select(task.id);
    requestAnimationFrame(() => {
      const target = document.getElementById("generation-progress");
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
      target?.focus({ preventScroll: true });
    });
  }
  async function start(selection: AnalysisSelection) {
    if (await analysis.start(selection)) setSetupOpen(false);
  }
  return (
    <>
      <div className="heading">
        <div>
          <button className="text-button detail-back" onClick={() => onBack()}>
            ← 返回
          </button>
          <h1>总结分析</h1>
        </div>
      </div>
      {targetNotice}
      {health && !health.model_configured && (
        <div className="notice">
          <strong>本地识别已就绪，总结需要配置模型 API。</strong>
          <span>
            打开“模型设置”配置并启用 API 连接后即可重试。
            <a href="#settings">前往设置 →</a>
          </span>
        </div>
      )}
      <div className="workspace">
        <div className="video-column">
          <section className="panel player-panel">
            <div className="panel-header">
              <h2>{video.filename}</h2>
              <span>{clock(video.duration)}</span>
            </div>
            <VideoPlayer
              key={video.id}
              id={video.id}
              playerRef={player}
              seek={jumpTime}
              onError={onError}
            />
            <div className="player-caption">
              <span>
                ◷{" "}
                {jumpTime !== null
                  ? `已定位至 ${clock(jumpTime)}`
                  : "点击时间戳，定位视频"}
              </span>
              <span>本地文件</span>
            </div>
            <div className="player-actions">
              <input
                ref={fileInput}
                type="file"
                accept={(health?.supported_video_extensions || [])
                  .map((ext) => `.${ext}`)
                  .join(",")}
                aria-label="重新选择本地文件"
                hidden
                disabled={busy || !health}
                onChange={(event) => {
                  void onUpload(event.target.files);
                  event.target.value = "";
                }}
              />
              <button
                className="secondary"
                disabled={busy}
                onClick={() => fileInput.current?.click()}
              >
                {uploadPercent !== null
                  ? `正在上传 ${uploadPercent}%`
                  : "重新选择文件"}
              </button>
              {(result || readingTask?.config?.outline_preview) && (
                <button
                  className="secondary return-to-notes"
                  onClick={returnToNotes}
                >
                  回到笔记 ↓
                </button>
              )}
            </div>
          </section>
          <TaskPanel
            task={analysis.receipt ? null : task}
            health={health}
            busy={busy || !!analysis.receipt || analysis.missing}
            syncing={!!analysis.receipt}
            missing={analysis.missing}
            retryRead={analysis.retryRead}
            reconnecting={analysis.reconnecting}
            onReadNotes={readCurrentNotes}
            retry={() => void analysis.control("retry")}
            controlTask={(action) => void analysis.control(action)}
            onStart={() => setSetupOpen(true)}
          />
        </div>
        <Notes
          key={`video/${video.id}/${readingTask?.id || "no-task"}`}
          reading={reading}
          versionReading={notes}
          evidenceReading={evidenceReading}
          result={result}
          task={readingTask}
          hasVersions={notes.hasVersions}
          onVersionChange={notes.select}
          latestVersionId={notes.latestId}
          previousVersionId={notes.previousId}
          onReturnToGeneration={returnToGeneration}
          processing={processing}
          availableEvidence={availableEvidence}
          jump={jump}
        />
        {(!!evidence.length || evidenceReading.loading || evidenceReading.error) && (
          <Transcript
            key={`transcript-${readingTask?.id || "no-task"}`}
            reading={evidenceReading}
            video={video}
            task={readingTask}
            evidence={evidence}
            availableEvidence={availableEvidence}
            jump={jump}
          />
        )}
      </div>
      {setupOpen && (
        <AnalysisSetupDialog
          videoId={video.id}
          filename={video.filename}
          busy={busy}
          onStart={start}
          onClose={() => setSetupOpen(false)}
          onSettings={() => {
            setSetupOpen(false);
            location.hash = "settings";
          }}
        />
      )}
    </>
  );
}
