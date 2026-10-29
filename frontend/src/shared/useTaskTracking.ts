import { useCallback, useEffect, useRef, useState } from "react";
import { errorText, ReportedError } from "../api";
import type { TaskReceipt } from "../types";
import { terminal, type TaskStatus } from "./taskStatus";

type TrackedTask = { id: string; status: TaskStatus; error: { message?: string; error_id?: string; log_file?: string } | null };

// 统一管理任务回执、串行读取及重连，业务 Hook 只提供请求和状态回调。
export function useTaskTracking<T extends TrackedTask>({ initial, read, onChanged, refresh, onError, onBusyChange }: {
  initial: T | null;
  read: (id: string, signal: AbortSignal) => Promise<T>;
  onChanged: (task: T) => void;
  refresh: () => Promise<unknown>;
  onError: (error: unknown) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [task, setTask] = useState(initial);
  const [receipt, setReceipt] = useState<Pick<TaskReceipt, "task_id"> | null>(null);
  const [busy, setBusy] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [missing, setMissing] = useState(false);
  const [revision, setRevision] = useState(0);
  const current = useRef(task);
  const accepted = useRef<Pick<TaskReceipt, "task_id"> | null>(null);
  const operating = useRef(false);
  const epoch = useRef(0);
  const lifetime = useRef(new AbortController());
  const polling = useRef<AbortController | null>(null);
  const callbacks = useRef({ read, onChanged, refresh, onError, onBusyChange });
  callbacks.current = { read, onChanged, refresh, onError, onBusyChange };

  useEffect(() => {
    lifetime.current = new AbortController();
    return () => {
      ++epoch.current;
      lifetime.current.abort();
      polling.current?.abort();
      callbacks.current.onBusyChange(false);
    };
  }, []);
  useEffect(() => { if (task) callbacks.current.onChanged(task); }, [task]);

  // 操作或任务切换时递增代次，让已发出的旧读取结果失效。
  const invalidate = useCallback(() => {
    ++epoch.current;
    polling.current?.abort();
    setReconnecting(false);
    setMissing(false);
  }, []);
  // 成功回执先接管为待确认任务，不依赖紧随其后的读取是否成功。
  const adopt = useCallback((value: Pick<TaskReceipt, "task_id">) => {
    invalidate();
    accepted.current = value;
    setReceipt(value);
    setRevision(value => value + 1);
  }, [invalidate]);

  useEffect(() => {
    if (operating.current || missing) return;
    const pending = accepted.current;
    const previous = current.current;
    const id = pending?.task_id || previous?.id;
    if (!id || !pending && previous && terminal.has(previous.status)) return;
    const controller = new AbortController();
    polling.current = controller;
    const version = epoch.current;
    let timer: ReturnType<typeof setTimeout>;
    let disconnected = false;
    const valid = () => !controller.signal.aborted && version === epoch.current;
    // 每次读取结束后再计时，断线只重读状态，不重复提交写请求。
    async function poll() {
      try {
        const latest = await callbacks.current.read(id!, controller.signal);
        if (!valid()) return;
        const before = current.current;
        const confirming = !!accepted.current;
        current.current = latest;
        accepted.current = null;
        setReceipt(null);
        setTask(latest);
        setReconnecting(false);
        disconnected = false;
        if (latest.error && (!before || before.id !== latest.id || !terminal.has(before.status)))
          callbacks.current.onError(new ReportedError(errorText(latest.error)));
        if (confirming || terminal.has(latest.status)) {
          void callbacks.current.refresh().catch(error => { if (valid()) callbacks.current.onError(error); });
        }
        if (!terminal.has(latest.status)) timer = setTimeout(poll, 2000);
      } catch (error) {
        if (!valid()) return;
        if (error instanceof ReportedError && error.status === 404) {
          setMissing(true);
          callbacks.current.onError(error);
          return;
        }
        setReconnecting(true);
        if (!disconnected) callbacks.current.onError(error);
        disconnected = true;
        timer = setTimeout(poll, 5000);
      }
    }
    if (pending) void poll();
    else timer = setTimeout(poll, 500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision, missing]);

  // 同步防重并暂停旧轮询；提交失败后继续跟踪原任务。
  async function submit(write: (signal: AbortSignal) => Promise<TaskReceipt>) {
    if (operating.current || accepted.current || missing) return false;
    operating.current = true;
    invalidate();
    const version = epoch.current;
    const signal = lifetime.current.signal;
    setBusy(true);
    callbacks.current.onBusyChange(true);
    callbacks.current.onError("");
    try {
      const value = await write(signal);
      if (signal.aborted || version !== epoch.current) return false;
      adopt(value);
      return true;
    } catch (error) {
      if (!signal.aborted && version === epoch.current) callbacks.current.onError(error);
      return false;
    } finally {
      operating.current = false;
      if (!signal.aborted) {
        setBusy(false);
        callbacks.current.onBusyChange(false);
        setRevision(value => value + 1);
      }
    }
  }
  return { task, receipt, busy, reconnecting, missing, submit, adopt,
    retryRead: () => { invalidate(); setRevision(value => value + 1); } };
}
