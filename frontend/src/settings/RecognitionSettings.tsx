import { notifyError, notifySuccess } from "../notifications";
import { useEffect, useRef, useState } from "react";
import { apiUrl, ReportedError, errorText, isAbortError, requestJSON } from "../api";

import type { RecognitionSelection, RecognitionModel, RecognitionCatalog } from "../types";

// 管理识别型号选择、文件状态与下载控制，关闭页面不取消后台下载。
export function RecognitionSettings({ kind, onSaved }: { kind: "asr" | "ocr"; onSaved: () => void }) {
  const [catalog, setCatalog] = useState<RecognitionCatalog | null>(null);
  const [selection, setSelection] = useState<RecognitionSelection | null>(null);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modelError, setModelError] = useState<{ id: string; message: string } | null>(null);
  const [downloadFeedback, setDownloadFeedback] = useState<{ id: string; message: string; state: string } | null>(null);
  const [removal, setRemoval] = useState<RecognitionModel | null>(null);
  const confirmation = useRef<HTMLDialogElement>(null);
  const seenDownloadErrors = useRef<Set<string> | null>(null);
  const lifetime = useRef(new AbortController());
  const revision = useRef(0);
  // 读取型号目录和当前选择，轮询只更新状态，不覆盖用户正在编辑的选择。
  async function load(initial = false, signal = lifetime.current.signal) {
    const version = ++revision.current;
    const result = await requestJSON<RecognitionCatalog>(apiUrl("/settings/recognition"), { signal });
    if (signal.aborted || version !== revision.current) return;
    const errors = kind === "asr" ? result.models.flatMap((model) => model.kind === "asr" && model.download?.error?.error_id ? [model.download.error] : []) : [];
    if (seenDownloadErrors.current === null) {
      seenDownloadErrors.current = new Set(errors.map((error) => error.error_id!));
    } else {
      for (const error of errors) {
        if (!seenDownloadErrors.current.has(error.error_id!)) {
          seenDownloadErrors.current.add(error.error_id!);
          notifyError(errorText(error));
        }
      }
    }
    setCatalog(result);
    setDownloadFeedback(current => {
      if (!current?.message) return current;
      const state = result.models.find(model => model.id === current.id)?.download;
      return (state?.error?.error_id || state?.status || "") === current.state ? current : null;
    });
    if (initial) setSelection(result.settings);
  }
  function failure(error: unknown, modelId?: string) {
    if (lifetime.current.signal.aborted || isAbortError(error)) return;
    setFailed(true);
    setMessage("");
    if (!(error instanceof ReportedError)) throw error;
    if (kind === "ocr" && modelId) setModelError({ id: modelId, message: error.message });
  }
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void load(true, controller.signal).catch(failure);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (kind !== "asr" || !catalog?.active_download || busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await load(false, controller.signal); }
      catch (error) { if (!controller.signal.aborted) failure(error); }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, 2000); }
    }
    timer = setTimeout(poll, 2000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [kind, catalog?.active_download, busy]);
  useEffect(() => {
    if (removal) confirmation.current?.showModal();
    return () => confirmation.current?.close();
  }, [removal]);
  if (!catalog || !selection) return failed
    ? null
    : <p role="status">正在读取本地模型…</p>;
  const field = kind === "asr" ? "asr_option" : "ocr_option";
  const models = catalog.models.filter((model) => model.kind === kind);
  const selected = models.find((model) => model.id === selection[field]);
  const saved = catalog.models.find((model) => model.id === catalog.settings[field]);
  // 按用户动作启动、暂停、继续或取消下载，操作结果再驱动目录刷新。
  async function download(action?: "pause" | "resume" | "cancel") {
    if (!selected) return;
    const signal = lifetime.current.signal;
    setBusy(true); setMessage(""); setFailed(false);
    const state = selected.download?.error?.error_id || selected.download?.status || "";
    setDownloadFeedback({ id: selected.id, message: "", state });
    try {
      await requestJSON(apiUrl(`/settings/models/${selected.id}/download${action ? `/${action}` : ""}`), { method: "POST", signal });
      if (signal.aborted) return;
      await load();
      if (!signal.aborted) setDownloadFeedback(null);
    } catch (error) {
      if (!signal.aborted && error instanceof ReportedError) setDownloadFeedback({ id: selected.id, message: error.message, state });
      failure(error);
    }
    finally { if (!signal.aborted) setBusy(false); }
  }
  // 保存已准备好的识别选择，成功后才通知应用刷新健康状态。
  async function save() {
    const signal = lifetime.current.signal;
    setBusy(true); setMessage(""); setFailed(false);
    try {
      await requestJSON(apiUrl("/settings/recognition"), {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection), signal,
      });
      if (signal.aborted) return;
      await load();
      if (signal.aborted) return;
      setModelError(null);
      setMessage("识别设置已保存。下一次分析使用新模型，运行中的任务继续使用原设置。模型改变后会重新识别，已有原始转录继续保留。");
      onSaved();
      notifySuccess(kind === "asr" ? "语音识别设置保存成功。" : "OCR 设置保存成功。");
    } catch (error) { failure(error, selected?.id); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function reveal() {
    if (!selected) return;
    const signal = lifetime.current.signal;
    setBusy(true); setMessage(""); setFailed(false);
    try {
      await requestJSON(apiUrl(`/settings/models/${selected.id}/reveal`), { method: "POST", signal });
      if (!signal.aborted) setModelError(null);
    } catch (error) { failure(error, selected.id); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  async function remove() {
    if (!removal) return;
    const signal = lifetime.current.signal;
    setBusy(true); setMessage(""); setFailed(false);
    try {
      await requestJSON(apiUrl(`/settings/models/${removal.id}`), { method: "DELETE", signal });
      if (signal.aborted) return;
      await load();
      if (signal.aborted) return;
      setRemoval(null);
      notifySuccess("模型文件已删除，需要使用时请重新下载。");
    } catch (error) { failure(error); }
    finally { if (!signal.aborted) setBusy(false); }
  }
  const downloadState = kind === "asr" ? selected?.download : null;
  const downloadFailed = ["failed", "interrupted"].includes(downloadState?.status || "");
  const downloadError = downloadFeedback?.id === selected?.id ? downloadFeedback?.message
    : downloadFailed && downloadState ? downloadState.error ? errorText(downloadState.error) : downloadState.message : "";
  const downloading = kind === "asr" && catalog.active_download === selected?.id;
  const resumable = kind === "asr" && ["paused", "interrupted"].includes(downloadState?.status || "");
  const changingDownload = ["pausing", "cancelling"].includes(downloadState?.status || "");
  return (
    <div className="settings-card">
      <h2>{kind === "asr" ? "语音识别 · 把讲解声音转成文字" : "OCR · 从截图中读取文字"}</h2>
      <p>{kind === "asr" ? "选择适合视频语言的模型。" : "在本机运行，不需要 API Token。OCR 只识别文字，不能完整理解图形、动作或公式。"}</p>
      <p>当前使用：<strong>{saved?.name || catalog.settings[field]}</strong></p>
      <label>
        {kind === "asr" ? "语音识别模型" : "OCR 模型"}
        <select aria-label={kind === "asr" ? "语音识别模型" : "OCR 模型"} value={selection[field]} onChange={(event) => {
          setSelection({ ...selection, [field]: event.target.value }); setMessage(""); setModelError(null);
        }}>
          {models.map((model) => <option key={model.id} value={model.id}>{model.name}{kind === "ocr" ? model.ready ? " · 已准备好" : " · 尚未准备好" : model.ready ? " · 已下载" : " · 未下载"}</option>)}
        </select>
      </label>
      {selected && <div className="local-model-detail">
        <h3>{selected.name}</h3>
        <p>{selected.description}</p>
        <dl>
          <div><dt>模型文件</dt><dd>{selected.ready ? "已准备好" : "尚未准备好"}</dd></div>
          <div><dt>文件大小</dt><dd>{selected.size}</dd></div>
        </dl>
        {downloadError && <p className="model-download-state task-error" role="alert">{downloadError}</p>}
        {downloadState && !downloadFailed && downloadState.status !== "succeeded" && <div className="model-download-state" role="status">
          <p>{downloadState.message}</p>
          {(downloading || resumable) && <>
            <progress max={downloadState.total_bytes || undefined} value={downloadState.total_bytes ? downloadState.downloaded_bytes : undefined} aria-label="模型下载进度" />
            <small>{downloadState.total_bytes ? `${Math.round(downloadState.downloaded_bytes / 1024 / 1024)} / ${Math.round(downloadState.total_bytes / 1024 / 1024)} MB` : "正在下载或校验，暂时无法计算百分比…"}</small>
          </>}
        </div>}
        {!selected.runtime_ready && <p className="notice" role="status">识别组件不可用，请检查程序安装后再保存。</p>}
        {/* 下载操作在卡片内并排；打开文件夹放在卡片下方，沿用现有按钮和间距。 */}
        {kind === "asr" && <div className="settings-actions local-model-actions">
          {downloading || resumable ? <>
            <button type="button" disabled={busy || changingDownload || (resumable && Boolean(catalog.active_download))}
              onClick={() => void download(resumable ? "resume" : "pause")}>
              {downloadState?.status === "pausing" ? "正在暂停…" : resumable ? "继续下载" : "暂停下载"}
            </button>
            <button type="button" disabled={busy || downloadState?.status === "cancelling"}
              onClick={() => void download("cancel")}>
              {downloadState?.status === "cancelling" ? "正在取消…" : "取消下载"}
            </button>
          </> : <button type="button" disabled={busy || Boolean(catalog.active_download)} onClick={() => void download()}>
            {downloading ? "正在下载…" : selected.ready ? "重新下载 / 检查文件" : "下载模型到 models"}
          </button>}
          {!downloading && !resumable &&
            <button type="button" disabled={busy || !selected.directory_exists || Boolean(catalog.active_download)}
              onClick={() => setRemoval(selected)}>删除模型文件</button>}
        </div>}
        {kind === "asr" && catalog.active_download && !downloading && <p>另一个模型正在下载，请等待完成。关闭设置窗口不会取消下载。</p>}
      </div>}
      {selected && <div className="settings-actions model-folder-action">
        <button type="button" disabled={busy || !selected.directory_exists} onClick={() => void reveal()}>打开模型文件夹</button>
      </div>}
      {modelError && modelError.id === selected?.id && <p className="task-error" role="alert">{modelError.message}</p>}
      {kind === "asr" && <label>
        视频语音语言
        <select value={selection.language} onChange={(event) => setSelection({ ...selection, language: event.target.value })}>
          <option value="zh">中文（普通话）</option><option value="en">英文</option><option value="auto">自动识别</option>
        </select>
        <small>Paraformer 适用于中文；Whisper 支持中文、英文等多种语言。</small>
      </label>}
      {!failed && message && <p role="status" className="notice">{message}</p>}
      <div className="settings-actions">
        <button type="button" className="primary" disabled={busy || !selected?.ready || !selected.runtime_ready || downloading || resumable} onClick={() => void save()}>保存识别设置</button>
      </div>
      <p>{kind === "ocr" ? "保存设置后，才会切换 OCR 模型。" : "下载完成后保存设置，才会切换模型。"}</p>
      {removal && <dialog className="file-edit-dialog" ref={confirmation} aria-labelledby="model-delete-title"
        onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!busy) setRemoval(null); }}>
        <form onSubmit={event => { event.preventDefault(); void remove(); }}>
          <h2 id="model-delete-title">删除模型文件？</h2>
          <p>将删除“{removal.name}”的本地模型文件夹及其中的文件，使用前需要重新下载。</p>
          {removal.id === catalog.settings.asr_option && <p>这是当前使用的语音识别模型。</p>}
          <div className="dialog-actions">
            <button type="button" disabled={busy} onClick={() => setRemoval(null)}>取消</button>
            <button type="submit" className="danger-button" disabled={busy}>{busy ? "正在删除…" : "确认删除"}</button>
          </div>
        </form>
      </dialog>}
    </div>
  );
}
