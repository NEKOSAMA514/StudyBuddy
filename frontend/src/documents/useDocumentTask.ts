import { useEffect, useRef } from "react";
import { api } from "../api";
import type { DocumentFile, DocumentTask, NoteStyle, TaskReceipt } from "../types";
import { useTaskTracking } from "../shared/useTaskTracking";

// 接管文档任务回执和提取完成事件，同一提取记录只交接一次。
export function useDocumentTask(document: DocumentFile, onChanged: (task: DocumentTask) => void,
  refresh: () => Promise<unknown>, onError: (error: unknown) => void, onBusyChange: (busy: boolean) => void,
  receipt?: Pick<TaskReceipt, "task_id">, onTaken?: () => void) {
  const tracking = useTaskTracking({ initial: document.latest_task || null,
    read: (id, signal) => api<DocumentTask>(`/document-tasks/${id}`, { signal, errorPolicy: "silent" }),
    onChanged, refresh, onError, onBusyChange });
  // 历史已完成提取记录不能覆盖后来手动创建的任务。
  const extraction = document.extraction;
  const handed = useRef(new Set<string>(extraction?.status === "done" && document.latest_task
    ? [`${extraction.source_id}/${extraction.task_id}`] : []));
  const { adopt } = tracking;
  useEffect(() => {
    if (!receipt) return;
    if (extraction) handed.current.add(`${extraction.source_id}/${extraction.task_id}`);
    adopt(receipt);
    onTaken?.();
  }, [receipt, adopt, onTaken]);
  useEffect(() => {
    if (extraction?.status !== "done" || !extraction.task_id) return;
    const key = `${extraction.source_id}/${extraction.task_id}`;
    if (handed.current.has(key)) return;
    handed.current.add(key);
    adopt({ task_id: extraction.task_id });
  }, [extraction?.status, extraction?.source_id, extraction?.task_id, adopt]);
  return { ...tracking,
    operate: (action: "start" | "stop" | "resume" | "retry", profileId?: string, noteStyle?: NoteStyle) =>
      tracking.submit(signal => api<TaskReceipt>(action === "start" ? `/documents/${document.id}/analyses`
        : `/document-tasks/${tracking.task!.id}/${action}`, { method: "POST", errorPolicy: "caller", signal, headers: { "Content-Type": "application/json" },
        ...(action === "start" ? { body: JSON.stringify({ llm_profile_id: profileId, note_style: noteStyle }) } : {}) })),
  };
}
