import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText, ReportedError, type RequestErrorPolicy } from "../api";
import type { DocumentExtraction, DocumentFile, DocumentTask, Health, LibraryFile, Task, TaskReceipt, Video } from "../types";
import { terminal } from "../shared/taskStatus";
import { FileEditDialog, type FileAction } from "./FileActions";
import { uploadFile } from "./upload";
import { useDocumentImport } from "../documents/useDocumentImport";

// 置顶优先，其后按最近任务动态排序，普通读取不会改变文件顺序依据。
function orderFiles(files: LibraryFile[]) {
  return files.sort(
    (a, b) =>
      Number(!!b.pinned) - Number(!!a.pinned) ||
      Date.parse(b.latest_task?.updated_at || b.created_at) -
        Date.parse(a.latest_task?.updated_at || a.created_at) ||
      Date.parse(b.created_at) - Date.parse(a.created_at) ||
      a.id.localeCompare(b.id),
  );
}

function withTask(file: LibraryFile, task: Task | DocumentTask): LibraryFile {
  if (file.kind === "document") return "document_id" in task && task.document_id === file.id ? { ...file, latest_task: task } : file;
  return "video_id" in task && task.video_id === file.id ? { ...file, latest_task: task } : file;
}

export function useFiles({
  health,
  activeFileId,
  blocked,
  onUploaded,
  onDeleted,
  onError,
}: {
  health: Health | null;
  activeFileId?: string;
  blocked: boolean;
  onUploaded: (file: LibraryFile, receipt?: Pick<TaskReceipt, "task_id">) => void;
  onDeleted: (id: string) => void;
  onError: (error: unknown) => void;
}) {
  const [library, setLibrary] = useState<LibraryFile[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [edit, setEdit] = useState<{
    file: LibraryFile;
    action: "rename" | "delete";
  } | null>(null);
  const inFlight = useRef(false);
  const active = useRef(activeFileId);
  const trackedTask = useRef<Task | DocumentTask | null>(null);
  if (active.current !== activeFileId) {
    active.current = activeFileId;
    trackedTask.current = null;
  }
  const refreshSequence = useRef(0);
  const reads = useRef<Promise<unknown>>(Promise.resolve());
  const mounted = useRef(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    mounted.current = true;
    return () => {
      mounted.current = false;
      lifetime.current.abort();
      ++refreshSequence.current;
    };
  }, []);

  // 所有列表读取进入同一串行队列，普通轮询排队时不作废当前响应。
  const readFiles = useCallback((errorPolicy: RequestErrorPolicy = "caller") => {
    const request = reads.current.catch(() => {}).then(async () => {
      if (!mounted.current) return [];
      const sequence = refreshSequence.current;
      setLoading(true);
      try {
        const signal = lifetime.current.signal;
        const results = await Promise.allSettled([api<Video[]>("/videos", { errorPolicy, signal }), api<DocumentFile[]>("/documents", { errorPolicy, signal })]);
        const [videos, documents] = results.map(result => {
          if (result.status === "rejected") throw result.reason;
          return result.value;
        });
        const entries: LibraryFile[] = [...videos, ...documents];
        if (mounted.current && sequence === refreshSequence.current) {
          // 详情的任务快照单向同步列表；列表请求不能让正在跟踪的任务倒退。
          const snapshot = trackedTask.current;
          setLibrary(
            orderFiles(
              entries.map((file) =>
                file.id === active.current && snapshot
                  ? withTask(file, snapshot)
                  : file,
              ),
            ),
          );
          setLoaded(true);
          setLoadError(false);
        }
        return entries;
      } catch (error) {
        if (mounted.current && sequence === refreshSequence.current) setLoadError(true);
        throw error;
      } finally {
        if (mounted.current && sequence === refreshSequence.current) setLoading(false);
      }
    });
    reads.current = request;
    return request;
  }, []);
  const refresh = useCallback((errorPolicy: RequestErrorPolicy = "caller") => {
    // 显式刷新使操作前的读取失效；普通轮询只排队，不作废当前响应。
    ++refreshSequence.current;
    return readFiles(errorPolicy);
  }, [readFiles]);
  const taskChanged = useCallback((task: Task | DocumentTask) => {
    trackedTask.current = task;
    setLibrary((files) =>
      orderFiles(
        files.map((file) =>
          withTask(file, task),
        ),
      ),
    );
  }, []);
  useEffect(() => {
    void readFiles().catch(onError);
  }, [readFiles, onError]);
  const backgroundProcessing = library.some(
    (file) =>
      file.id !== activeFileId &&
      (file.latest_task && !terminal.has(file.latest_task.status) || file.kind === "document" &&
        file.extraction && ["queued", "running"].includes(file.extraction.status)),
  );
  useEffect(() => {
    if (!backgroundProcessing) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await readFiles("silent"); } catch { /* 保留上次成功列表，并显示手动重读入口。 */ }
      if (!stopped) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 2000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [backgroundProcessing, readFiles, onError]);

  // 上传、提取和文件写操作共用同步防重入口，避免一次渲染内重复提交。
  const beginOperation = useCallback(() => {
    if (blocked || inFlight.current) return false;
    inFlight.current = true;
    setBusy(true);
    return true;
  }, [blocked]);
  const endOperation = useCallback(() => { inFlight.current = false; setBusy(false); }, []);
  const extractionChanged = useCallback((id: string, extraction: DocumentExtraction) => {
    setLibrary(files => files.map(file => file.kind === "document" && file.id === id ? { ...file, extraction } : file));
  }, []);
  const readImportFiles = useCallback(() => readFiles("silent"), [readFiles]);
  const importing = useDocumentImport({ targetId: activeFileId, target: library.find(file => file.id === activeFileId),
    allowMissingRecovery: loaded && !loadError, readFiles: readImportFiles, refresh, beginOperation, endOperation,
    onState: extractionChanged, onReady: onUploaded, onError });

  // 校验单个文件后上传并刷新列表，文档只保存原件，提取由详情页发起。
  async function upload(files: FileList | null) {
    if (blocked || inFlight.current || !files?.length) return;
    if (files.length !== 1) {
      onError(new ReportedError("请一次选择一个文件。"));
      return;
    }
    const file = files[0];
    const extension = file.name.split(".").pop()!.toLowerCase();
    const document = (health?.supported_document_extensions || []).includes(extension);
    if (!document && !(health?.supported_video_extensions || []).includes(extension)) {
      onError(new ReportedError("请选择支持的视频或文档文件。"));
      return;
    }
    if (health && file.size > (document ? health.max_document_bytes || health.max_upload_bytes : health.max_upload_bytes)) {
      onError(new ReportedError("文件超过大小限制，请选择更小的文件。"));
      return;
    }
    if (!beginOperation()) return;
    onError("");
    setUploadPercent(0);
    try {
      const uploaded = await uploadFile(file, setUploadPercent, document);
      await refresh();
      onUploaded(uploaded);
      importing.uploaded(uploaded);
    } catch (error) {
      onError(error);
    } finally {
      endOperation();
      setUploadPercent(null);
    }
  }

  // 统一执行重命名、置顶和删除，写入后显式刷新以隔离操作前的读取。
  async function act(file: LibraryFile, action: FileAction, name?: string) {
    if (blocked || inFlight.current) return;
    if (action !== "pin" && name === undefined) {
      onError("");
      setEdit({ file, action });
      return;
    }
    if (!beginOperation()) return;
    onError("");
    const endpoint = `/${file.kind === "document" ? "documents" : "videos"}/${file.id}`;
    try {
      if (action === "delete") {
        const removed = await api<{
          warning?: { message: string; error_id?: string; log_file?: string };
        }>(endpoint, { method: "DELETE", errorPolicy: "caller" });
        await refresh();
        onDeleted(file.id);
        if (removed.warning)
          onError(new ReportedError(errorText(removed.warning)));
      } else {
        await api<LibraryFile>(endpoint, {
          method: "PATCH", errorPolicy: "caller",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            action === "pin" ? { pinned: !file.pinned } : { filename: name },
          ),
        });
        await refresh();
      }
      setEdit(null);
    } catch (error) {
      onError(error);
    } finally {
      endOperation();
    }
  }
  return {
    remember: (file: LibraryFile) => setLibrary(files => orderFiles([...files.filter(item => item.id !== file.id), file])),
    library,
    loaded,
    loading,
    loadError,
    busy,
    uploadPercent,
    refresh,
    taskChanged,
    upload,
    act,
    processDocument: importing.processDocument,
    importStage: importing.importStage,
    importError: importing.importError,
    extractionReconnecting: importing.extractionReconnecting,
    resolvingImport: importing.resolvingImport,
    dialog: edit && (
      <FileEditDialog
        key={`${edit.file.id}-${edit.action}`}
        file={edit.file}
        action={edit.action}
        busy={busy || blocked}
        onClose={() => setEdit(null)}
        onConfirm={(name) => act(edit.file, edit.action, name)}
      />
    ),
  };
}
