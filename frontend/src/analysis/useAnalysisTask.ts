import { useEffect } from "react";
import { api } from "../api";
import type { Task, TaskReceipt, Video } from "../types";
import type { AnalysisSelection } from "../types";
import { useTaskTracking } from "../shared/useTaskTracking";
import { createAnalysis } from "./requests";

// 封装视频任务的提交参数与控制接口，回执和轮询复用共用跟踪逻辑。
export function useAnalysisTask({ video, blocked, onTaskChanged, refresh, onError, onBusyChange, receipt, onTaken }: {
  video: Video;
  blocked: boolean;
  onTaskChanged: (task: Task) => void;
  refresh: () => Promise<unknown>;
  onError: (error: unknown) => void;
  onBusyChange: (busy: boolean) => void;
  receipt?: Pick<TaskReceipt, "task_id">;
  onTaken?: () => void;
}) {
  const tracking = useTaskTracking({ initial: video.latest_task || null,
    read: (id, signal) => api<Task>(`/tasks/${id}`, { signal, errorPolicy: "silent" }),
    onChanged: onTaskChanged, refresh, onError, onBusyChange });
  const { adopt } = tracking;
  useEffect(() => { if (receipt) { adopt(receipt); onTaken?.(); } }, [receipt, adopt, onTaken]);
  return { ...tracking,
    start: (selection: AnalysisSelection) => blocked ? Promise.resolve(false)
      : tracking.submit(signal => createAnalysis(video.id, selection, signal)),
    control: (action: "stop" | "resume" | "retry") => !tracking.task || blocked ? Promise.resolve(false)
      : tracking.submit(signal => api<TaskReceipt>(`/tasks/${tracking.task!.id}/${action}`, { method: "POST", errorPolicy: "caller", signal })),
  };
}
