import { useEffect, useRef, useState } from "react";
import { api } from "../api";

type Target = { chapter_id: string; heading_index: number };
const targetKey = (chapter: string, heading: number) => JSON.stringify([chapter, heading]);

// 按笔记版本隔离重点标记，每个目标独立防重，写入成功后才更新展示。
export function useNoteMarks(kind: "video" | "document", taskId: string | undefined, enabled: boolean) {
  const identity = enabled && taskId ? `${kind}/${taskId}` : "";
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([identity, attempt]);
  const [saved, setSaved] = useState<{ identity: string; marks: string[] } | null>(null);
  const [read, setRead] = useState<{ key: string; error?: string } | null>(null);
  const [operations, setOperations] = useState<{ identity: string; targets: Record<string, { pending: boolean; error?: string }> } | null>(null);
  const writes = useRef(new Map<string, AbortController>());
  useEffect(() => () => {
    writes.current.forEach(controller => controller.abort());
    writes.current.clear();
  }, [identity]);
  useEffect(() => {
    if (!identity) return;
    const controller = new AbortController();
    api<{ marks: Target[] }>(`/note-marks/${identity}`, { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      setSaved({ identity, marks: value.marks.map(mark => targetKey(mark.chapter_id, mark.heading_index)) });
      setRead({ key });
    }).catch(error => {
      if (!controller.signal.aborted) setRead({ key, error: error instanceof Error ? error.message : "重点标记读取失败。" });
    });
    return () => controller.abort();
  }, [identity, key]);
  const ready = !!identity && read?.key === key && !read.error;
  const marks = saved?.identity === identity ? saved.marks : [];
  const targets = operations?.identity === identity ? operations.targets : {};
  function operation(target: string, state: { pending: boolean; error?: string }) {
    setOperations(previous => ({ identity, targets: { ...(previous?.identity === identity ? previous.targets : {}), [target]: state } }));
  }
  // 提交目标的期望标记状态，切换版本后取消旧请求并忽略迟到响应。
  async function toggle(chapter: string, heading: number) {
    const target = targetKey(chapter, heading);
    if (!ready || writes.current.has(target)) return;
    const controller = new AbortController();
    writes.current.set(target, controller);
    operation(target, { pending: true });
    try {
      const value = await api<Target & { marked: boolean }>(`/note-marks/${identity}`, {
        method: "PUT", signal: controller.signal, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chapter_id: chapter, heading_index: heading, marked: !marks.includes(target) }),
      });
      if (controller.signal.aborted) return;
      setSaved(previous => previous?.identity === identity ? { identity,
        marks: [...previous.marks.filter(item => item !== target), ...(value.marked ? [target] : [])] } : previous);
      operation(target, { pending: false });
    } catch (error) {
      if (!controller.signal.aborted) operation(target, { pending: false,
        error: error instanceof Error ? error.message : "重点标记保存失败。" });
    } finally {
      if (writes.current.get(target) === controller) writes.current.delete(target);
    }
  }
  return { enabled: !!identity, ready, marks, targets, toggle,
    notice: identity && (read?.key !== key ? <p className="muted note-marks-loading" role="status">正在读取重点标记…</p>
      : read.error ? <div className="reader-state" role="status"><span>{read.error}</span>
        <button className="secondary" onClick={() => setAttempt(value => value + 1)}>重新读取重点标记</button></div> : null) };
}

// 展示单个章节或小节的标记状态与保存反馈，不改写笔记正文。
export function NoteMarkButton({ state, chapter, heading = 0 }: {
  state: ReturnType<typeof useNoteMarks>; chapter: string; heading?: number;
}) {
  if (!state.enabled) return null;
  const target = targetKey(chapter, heading);
  const marked = state.marks.includes(target);
  const operation = state.targets[target];
  return <span className="note-mark-control">
    <button type="button" className="note-mark-button" aria-pressed={marked}
      aria-label={marked ? "取消重点" : "标为重点"} disabled={!state.ready} aria-disabled={!state.ready || !!operation?.pending}
      onClick={() => void state.toggle(chapter, heading)}>
      <span aria-hidden="true">{marked ? "★" : "☆"}</span> {operation?.pending ? "保存中…" : marked ? "重点" : "标为重点"}
    </button>
    {operation?.error && <span className="note-mark-error" role="status">{operation.error} 请再次点击重试。</span>}
  </span>;
}
