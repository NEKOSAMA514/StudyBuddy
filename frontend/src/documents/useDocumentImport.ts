import { useEffect, useRef, useState } from "react";
import { api, ReportedError } from "../api";
import { notifyInfo } from "../notifications";
import type { DocumentExtraction, DocumentFile, DocumentImportSelection, LibraryFile, TaskReceipt } from "../types";
import { mineruHint, mineruTokenURL } from "./mineru";

const pending = (value?: DocumentExtraction | null) => !!value && ["queued", "running", "interrupted"].includes(value.status);

// 协调文档提取和材料身份交接，列表读取仍交给调用方的唯一队列。
export function useDocumentImport({ targetId, target, allowMissingRecovery, readFiles, refresh, beginOperation, endOperation, onState, onReady, onError }: {
  targetId?: string; target?: LibraryFile; allowMissingRecovery: boolean;
  readFiles: () => Promise<LibraryFile[]>; refresh: () => Promise<unknown>;
  beginOperation: () => boolean; endOperation: () => void;
  onState: (id: string, extraction: DocumentExtraction) => void;
  onReady: (file: LibraryFile, receipt?: Pick<TaskReceipt, "task_id">) => void;
  onError: (error: unknown) => void;
}) {
  const [operation, setOperation] = useState({ id: "", stage: "", error: "" });
  const [connectionId, setConnectionId] = useState("");
  const [missing, setMissing] = useState("");
  const current = useRef({ targetId, onState, onReady });
  current.current = { targetId, onState, onReady };
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);

  // 只提交一次提取请求，记录回执后通过状态读取等待材料和笔记任务。
  async function processDocument(document: DocumentFile, selection: Partial<DocumentImportSelection>) {
    if (!beginOperation()) return false;
    const signal = lifetime.current.signal;
    setOperation({ id: document.id, stage: "正在提取文档，内嵌图片按需 OCR…", error: "" });
    onError("");
    try {
      const extraction = await api<DocumentExtraction>(`/documents/${document.extraction?.source_id || document.id}/extract`, {
        method: "POST", errorPolicy: "caller", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection) });
      if (signal.aborted) return false;
      onState(document.id, extraction);
      void refresh().catch(onError);
      return true;
    } catch (error) {
      if (!signal.aborted) {
        setOperation({ id: document.id, stage: "", error: error instanceof Error ? error.message : String(error) });
        onError(error);
      }
      return false;
    } finally {
      endOperation();
      if (!signal.aborted) setOperation(value => ({ ...value, stage: "" }));
    }
  }

  const watchId = targetId && targetId !== missing && (!target && allowMissingRecovery || target?.kind === "document" && pending(target.extraction)) ? targetId : "";
  const source = target?.kind === "document" ? target.extraction?.source_id || watchId : watchId;
  useEffect(() => {
    if (!watchId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let observedPending = target?.kind === "document" && pending(target.extraction);
    async function poll() {
      try {
        const value = await api<DocumentExtraction>(`/documents/${source}/extraction`, { signal: controller.signal, errorPolicy: "silent" });
        if (controller.signal.aborted || current.current.targetId !== watchId) return;
        setConnectionId("");
        if (pending(value)) observedPending = true;
        // 材料可先于笔记任务就绪；只有本次观察到的完成事件才携带任务回执。
        if (value.document_id) {
          const files = await readFiles();
          if (controller.signal.aborted || current.current.targetId !== watchId) return;
          const prepared = files.find(file => file.id === value.document_id);
          current.current.onState(value.document_id, value);
          if (prepared && prepared.id !== watchId) current.current.onReady({ ...prepared, extraction: value } as DocumentFile,
            observedPending && value.status === "done" && value.task_id ? { task_id: value.task_id } : undefined);
        } else current.current.onState(watchId, value);
        if (["done", "failed"].includes(value.status)) return;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (!target && error instanceof ReportedError && error.status === 404) { setMissing(watchId); return; }
        setConnectionId(watchId);
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1500);
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
    // 身份及跟踪条件决定生命周期，进度更新不重新创建读取循环。
  }, [watchId, source, readFiles]);

  // PDF 上传后按需提示免费凭据入口，静默读取失败不影响文档使用。
  function uploaded(file: LibraryFile) {
    setOperation(value => ({ ...value, error: "" }));
    if (file.kind !== "document" || file.format !== "pdf") return;
    const signal = lifetime.current.signal;
    void api<{ has_token: boolean }>("/settings/mineru", { signal, errorPolicy: "silent" }).then(value => {
      if (!signal.aborted && !value.has_token) notifyInfo(mineruHint, { href: mineruTokenURL, label: "免费申请 MinerU Token ↗" });
    }).catch(() => {});
  }
  return { processDocument, uploaded,
    importStage: operation.id === targetId ? operation.stage : "",
    importError: operation.id === targetId ? operation.error : "",
    extractionReconnecting: connectionId === targetId,
    resolvingImport: !!watchId && !target };
}
