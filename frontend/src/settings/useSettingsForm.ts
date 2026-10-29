import { useEffect, useRef, useState } from "react";
import { api, isAbortError } from "../api";
import { notifyError } from "../notifications";
import { useScopedRead } from "../shared/useScopedRead";

const showError = (error: unknown) => { if (!isAbortError(error)) notifyError(error instanceof Error ? error.message : String(error)); };

// 复用简单设置的读取与保存生命周期，将编辑草稿和服务器结果分开管理。
export function useSettingsForm<T>(path: string, onSaved: (value: T) => void) {
  const reading = useScopedRead<T>({ identity: path, path, errorMessage: "设置读取失败，请重新读取。", onError: showError });
  const [draft, setDraft] = useState<{ path: string; value: T } | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    inFlight.current = false;
    setBusy(false);
    return () => controller.abort();
  }, [path]);
  useEffect(() => {
    if (reading.data !== null) setDraft({ path, value: reading.data });
  }, [path, reading.data]);
  const value = draft?.path === path ? draft.value : null;
  // 捕获本次草稿并同步防重，仅成功后更新表单，关闭后的结果不再发布。
  async function save() {
    if (value === null || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const signal = lifetime.current.signal;
    try {
      const result = await api<T>(path, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value), signal, errorPolicy: "caller" });
      if (signal.aborted) return;
      setDraft({ path, value: result });
      onSaved(result);
    } catch (error) {
      if (!signal.aborted) showError(error);
    } finally {
      if (!signal.aborted) { inFlight.current = false; setBusy(false); }
    }
  }
  return { draft: value, setDraft: (value: T) => setDraft({ path, value }), busy, save, reading };
}
