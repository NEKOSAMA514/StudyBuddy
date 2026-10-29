import { useScopedRead } from "../shared/useScopedRead";
import { useNoteResult } from "../notes/useNoteResult";
import type { Evidence, Result, Task } from "../types";

// 分别读取所选版本的正文与依据，只展示结果实际引用的有效证据。
export function useAnalysisContent(
  videoId: string,
  task: Task | null,
  onError: (error: unknown) => void,
) {
  const scope = `${videoId}/${task?.id || ""}`;
  const reading = useNoteResult<Result>({ identity: `video/${scope}`, path: `/analyses/${task?.id || ""}`,
    revision: task?.notes_revision || 0, enabled: !!task && task.notes_state !== "none", onError });
  const sources = useScopedRead<Evidence[]>({ identity: scope,
    path: `/videos/${videoId}/evidence${task ? `?task_id=${task.id}` : ""}`,
    refreshKey: JSON.stringify([task?.status, task?.notes_revision]), errorMessage: "依据读取失败。", onError });
  const result = reading.result?.format_version === 4 ? reading.result : null;
  const evidence = sources.data || [];
  const availableEvidence = evidence
    .filter((item) => (result ? result.evidence_ids.includes(item.id) : true))
    .sort(
      (a, b) =>
        a.start_seconds - b.start_seconds ||
        a.end_seconds - b.end_seconds ||
        a.id.localeCompare(b.id),
    );
  return { result, reading, evidence, availableEvidence, evidenceReading: {
    loading: sources.loading, error: sources.error, retry: sources.retry,
  } };
}
