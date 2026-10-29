import { useEffect, useState } from "react";
import { useScopedRead } from "../shared/useScopedRead";

type VersionTask = { id: string; status: string; notes_state: string; updated_at: string };

// 管理两份成功笔记与当前草稿的选择，刷新失败时保留已读版本。
export function useNoteVersions<T extends VersionTask>({ scope, path, current, timestamp, onError, requestedId, navigationKey }: {
  scope: string; path: string; current: T | null; timestamp: (task: T) => string;
  onError: (error: unknown) => void; requestedId?: string; navigationKey?: string;
}) {
  const [selected, setSelected] = useState<{ scope: string; task: T } | null>(null);
  const reading = useScopedRead<T[]>({ identity: scope, path,
    refreshKey: JSON.stringify([current?.id, current?.status, requestedId]),
    errorMessage: "版本列表读取失败。", onError });
  const { confirmed } = reading;
  const versionsBeforeRefresh = reading.data || [];
  let completed = versionsBeforeRefresh;
  if (current?.status === "succeeded" && current.notes_state === "ready") {
    completed = [current, ...completed.filter(item => item.id !== current.id)]
      .sort((a, b) => timestamp(b).localeCompare(timestamp(a)) || b.id.localeCompare(a.id)).slice(0, 2);
  }
  // 只有版本列表本轮读取成功，才合入当前成功任务并确认旧版本已被淘汰。
  const versions = confirmed ? completed : versionsBeforeRefresh;
  const draft = current && current.status !== "succeeded" ? current : null;
  const choices = [...versions, ...(draft ? [draft] : [])];
  if (current && !choices.some(item => item.id === current.id)) choices.push(current);
  const following = draft?.notes_state === "partial" ? draft : completed[0] || current;
  const selectionScope = JSON.stringify([scope, navigationKey || requestedId || ""]);
  const selection = selected?.scope === selectionScope ? selected.task : null;
  const chosen = choices.find(item => item.id === selection?.id);
  const requested = requestedId ? versions.find(item => item.id === requestedId) : null;
  const automatic = requestedId ? requested || null : following;
  const task = chosen || (selection && !confirmed ? selection : automatic);
  useEffect(() => {
    if (confirmed && selection && !chosen) setSelected(null);
  }, [confirmed, selection, chosen]);
  return {
    task, versions, hasVersions: !!choices.length || !!selection,
    requestedUnavailable: !!requestedId && confirmed && !requested && !selection,
    latestId: versions[0]?.id, previousId: versions[1]?.id,
    select: (id: string) => {
      const item = choices.find(value => value.id === id);
      if (item) setSelected({ scope: selectionScope, task: item });
    },
    loading: reading.loading, error: reading.error, retry: reading.retry,
  };
}
