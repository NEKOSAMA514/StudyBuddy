import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DocumentUnit } from "../types";

type Content = { units: DocumentUnit[]; total: number; offset: number };
// 分页读取提取材料，来源跳转等待目标分组渲染后再定位。
export function useDocumentPreview(id: string, taskId: string | undefined, onError: (error: unknown) => void, enabled = true) {
  const [content, setContent] = useState<Content | null>(null);
  const [offset, setOffset] = useState(0);
  const [target, setTarget] = useState("1");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const [pending, setPending] = useState<{ id: string; group: number } | null>(null);
  const preview = useRef<HTMLElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setContent(null);
    if (!enabled) { setLoading(false); return () => controller.abort(); }
    api<Content>(`/documents/${id}/content?offset=${offset}&limit=10`, { errorPolicy: "caller", signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setContent(value); })
      .catch(error => { if (!controller.signal.aborted) onError(error); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, offset, retry, onError, enabled]);
  useEffect(() => { setPending(null); }, [taskId]);
  useEffect(() => {
    if (!expanded || loading || !pending || content?.offset !== pending.group) return;
    const element = globalThis.document.getElementById(`document-unit-${pending.id}`);
    element?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    element?.focus({ preventScroll: true });
    setPending(null);
  }, [pending, content, loading, expanded]);
  function jump(next: number) {
    setPending(null);
    setOffset(next);
    setTarget(String(next + 1));
    preview.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  // 将来源编号换算到对应分页，并记录渲染完成后需要聚焦的原文块。
  function jumpSource(unitId: string) {
    setExpanded(true);
    const index = Number(unitId.slice(1)) - 1;
    const group = Math.floor(index / 10) * 10;
    setOffset(group);
    setTarget(String(index + 1));
    setPending({ id: unitId, group });
  }
  return { content, offset, target, setTarget, loading, expanded, setExpanded, retry: () => setRetry(v => v + 1), preview, jump, jumpSource };
}
